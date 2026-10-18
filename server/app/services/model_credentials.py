"""Credential versions of model configurations (W1, architecture §7): write, clear, revoke, decrypt, rekey.

A configuration's key is either an environment reference (``env:NAME``; the value never enters the
database) or a direct key stored as AES-256-GCM ciphertext (ai/credentials.py). Every change writes a
new credential version; the previous one becomes ``replaced`` (old snapshots keep using it), while
``cleared`` / ``revoked`` stop every use and wipe the ciphertext (clearing also revokes every replaced
version, so no earlier key of the config stays usable). ``config_version`` is bumped by every
change that affects the connection, so snapshots and connection tests can tell they are outdated.

No function here returns, logs or audits a key: audit rows carry metadata only (source, change, version).

CLI (run in server/, uses the deployment's database and master keys)::

    uv run python -m app.services.model_credentials check   # key ids, counts per master key, decryptable or not
    uv run python -m app.services.model_credentials rekey   # re-encrypt every stored key with the current master key
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..ai import credentials as crypto
from ..ai.providers.base import ProviderError, resolve_secret
from ..errors import AppError
from ..models import ModelConfigRow, ModelCredential, ModelCredentialMeta, ModelEnvGeneration

#: Statuses of a credential version that still authorise frozen snapshots.
USABLE = ("active", "replaced")


def _now() -> datetime:
    return datetime.now(UTC)


def credential(db: Session, config_id: uuid.UUID | str, version: int | None) -> ModelCredential | None:
    if version is None:
        return None
    return db.scalar(select(ModelCredential).where(ModelCredential.model_config_id == uuid.UUID(str(config_id)),
                                                   ModelCredential.version == version))


def versions(db: Session, config_id: uuid.UUID) -> list[ModelCredential]:
    return list(db.scalars(select(ModelCredential).where(ModelCredential.model_config_id == config_id)
                           .order_by(ModelCredential.version.desc())).all())


def _next_version(db: Session, config_id: uuid.UUID) -> int:
    top = db.scalar(select(func.max(ModelCredential.version)).where(ModelCredential.model_config_id == config_id))
    return int(top or 0) + 1


def ensure_current(db: Session, row: ModelConfigRow) -> ModelCredential | None:
    """The active credential row of a config. A config written without one (seed data, rows inserted
    directly) gets its environment reference recorded as a new version here."""
    if row.credential_status != "active":
        return None
    cred = credential(db, row.id, row.credential_version)
    if cred is not None:
        return cred
    if row.credential_source != "env" or not row.secret_ref:
        return None
    try:
        with db.begin_nested():
            cred = ModelCredential(model_config_id=row.id, version=_next_version(db, row.id), source="env",
                                   env_ref=row.secret_ref, status="active")
            db.add(cred)
            db.flush()
    except IntegrityError:  # recorded concurrently: use that one
        cred = db.scalar(select(ModelCredential).where(ModelCredential.model_config_id == row.id, ModelCredential.status == "active")
                         .order_by(ModelCredential.version.desc()).limit(1))
        if cred is None:
            raise
    row.credential_version = cred.version
    return cred


def _retire_current(db: Session, row: ModelConfigRow, status: str) -> ModelCredential | None:
    """Mark the active version replaced / cleared / revoked (cleared and revoked wipe a stored key)."""
    cred = credential(db, row.id, row.credential_version)
    if cred is None or cred.status != "active":
        return None
    _set_status(cred, status)
    return cred


def _set_status(cred: ModelCredential, status: str) -> None:
    now = _now()
    cred.status = status
    if status == "replaced":
        cred.replaced_at = now
    elif status in ("cleared", "revoked"):
        if status == "cleared":
            cred.cleared_at = now
        else:
            cred.revoked_at = now
        cred.nonce = None
        cred.ciphertext = None


def set_env(db: Session, row: ModelConfigRow, env_ref: str, *, user_id: uuid.UUID | None) -> dict[str, Any] | None:
    """Use an environment reference. Returns the audit metadata of the change (None when nothing changed)."""
    current = credential(db, row.id, row.credential_version) if row.credential_status == "active" else None
    if current is not None and current.source == "env" and current.env_ref == env_ref and row.secret_ref == env_ref:
        return None
    previous = _retire_current(db, row, "replaced")
    cred = ModelCredential(model_config_id=row.id, version=_next_version(db, row.id), source="env", env_ref=env_ref,
                           status="active", created_by=user_id)
    db.add(cred)
    db.flush()
    row.credential_source, row.secret_ref, row.credential_version, row.credential_status = "env", env_ref, cred.version, "active"
    return {"source": "env", "change": "replaced" if previous else "set", "version": cred.version,
            "previous_version": previous.version if previous else None}


def set_direct(db: Session, row: ModelConfigRow, api_key: str, *, user_id: uuid.UUID | None) -> dict[str, Any]:
    """Encrypt and store a direct key as a new version (the master key must be configured: 503 master_key_missing)."""
    try:
        ring = crypto.require_keyring()
    except ProviderError as exc:
        raise AppError(503, "master_key_missing", exc.message) from None
    if row.id is None:
        row.id = uuid.uuid4()
    version = _next_version(db, row.id)
    sealed = crypto.encrypt(api_key, config_id=str(row.id), credential_version=version, ring=ring)
    previous = _retire_current(db, row, "replaced")
    cred = ModelCredential(model_config_id=row.id, version=version, source="direct", algorithm=sealed.algorithm,
                           cipher_format=sealed.cipher_format, key_version=sealed.key_version, nonce=sealed.nonce,
                           ciphertext=sealed.ciphertext, status="active", created_by=user_id)
    db.add(cred)
    db.flush()
    row.credential_source, row.secret_ref, row.credential_version, row.credential_status = "direct", None, version, "active"
    return {"source": "direct", "change": "replaced" if previous else "set", "version": version,
            "previous_version": previous.version if previous else None}


def clear(db: Session, row: ModelConfigRow) -> dict[str, Any] | None:
    """Remove the current key: the config stays unusable until a new key is set; snapshots of it stop.

    Clearing leaves no key of the config usable: every earlier replaced version (still serving snapshots
    frozen before a replacement) is revoked too, with its ciphertext wiped."""
    older = [c for c in versions(db, row.id) if c.status == "replaced"]
    if row.credential_status != "active" and not older:
        return None
    cred = _retire_current(db, row, "cleared") if row.credential_status == "active" else None
    for c in older:
        _set_status(c, "revoked")
    if row.credential_status == "active":
        row.credential_version, row.credential_status = None, "cleared"
        if row.credential_source == "env":
            row.secret_ref = None
    return {"source": row.credential_source, "change": "cleared", "version": cred.version if cred else None,
            "revoked_versions": sorted(c.version for c in older)}


def revoke(db: Session, row: ModelConfigRow, version: int | None) -> dict[str, Any] | None:
    """Revoke one credential version, or every still usable one (``version`` None): active and replaced versions.
    Revoked versions stop authorising any snapshot at once; the record stays as history without its ciphertext
    (a cleared version is already unusable and keeps its status)."""
    targets = [c for c in versions(db, row.id) if c.status in USABLE and (version is None or c.version == version)]
    if version is not None and not targets:
        known = credential(db, row.id, version)
        if known is None:
            raise AppError(404, "not_found", "credential version not found")
        return None
    if not targets:
        return None
    for c in targets:
        _set_status(c, "revoked")
    if row.credential_version is not None and any(c.version == row.credential_version for c in targets):
        row.credential_version, row.credential_status = None, "revoked"
        if row.credential_source == "env":
            row.secret_ref = None
    return {"change": "revoked", "versions": sorted(c.version for c in targets)}


def decrypt(cred: ModelCredential) -> str:
    """The plaintext of a direct credential (short-lived: only the resolver and the provider client hold it)."""
    return crypto.decrypt(algorithm=cred.algorithm, cipher_format=cred.cipher_format, key_version=cred.key_version,
                          nonce=cred.nonce, ciphertext=cred.ciphertext, config_id=str(cred.model_config_id),
                          credential_version=cred.version)


# --- environment generations ---------------------------------------------------------------------


def _salt(db: Session) -> bytes:
    meta = db.get(ModelCredentialMeta, 1)
    if meta is not None:
        return meta.fingerprint_salt
    try:
        with db.begin_nested():
            db.add(ModelCredentialMeta(id=1, fingerprint_salt=os.urandom(32)))
            db.flush()
    except IntegrityError:
        pass
    meta = db.get(ModelCredentialMeta, 1)
    assert meta is not None
    return meta.fingerprint_salt


def env_generation(db: Session, env_ref: str, value: str, *, record: bool = True) -> int | None:
    """Generation number of the value behind ``env_ref`` (a new value gets the next number when ``record``).
    None when not recorded yet and ``record`` is False. The fingerprint never leaves this function's caller."""
    fp = crypto.fingerprint(_salt(db), env_ref, value)
    found = db.scalar(select(ModelEnvGeneration.generation).where(ModelEnvGeneration.env_ref == env_ref,
                                                                 ModelEnvGeneration.fingerprint == fp))
    if found is not None or not record:
        return found
    for _ in range(3):
        try:
            with db.begin_nested():
                top = db.scalar(select(func.max(ModelEnvGeneration.generation)).where(ModelEnvGeneration.env_ref == env_ref))
                db.add(ModelEnvGeneration(env_ref=env_ref, fingerprint=fp, generation=int(top or 0) + 1))
                db.flush()
        except IntegrityError:
            pass
        found = db.scalar(select(ModelEnvGeneration.generation).where(ModelEnvGeneration.env_ref == env_ref,
                                                                     ModelEnvGeneration.fingerprint == fp))
        if found is not None:
            return found
    raise ProviderError("unknown", "could not record the environment key generation", retryable=True)


def current_env_generation(db: Session, env_ref: str, *, record: bool = True) -> int | None:
    """Generation of the value the server sees now (None when the variable is not set)."""
    try:
        value = resolve_secret(env_ref)
    except ProviderError:
        return None
    return env_generation(db, env_ref, value, record=record)


# --- master key rotation --------------------------------------------------------------------------


def rekey(db: Session) -> dict[str, Any]:
    """Re-encrypt every stored key with the current master key (same plaintext, same credential version).
    Rows whose master key is not configured are reported, not touched. Never outputs a key."""
    ring = crypto.require_keyring()
    rows = list(db.scalars(select(ModelCredential).where(ModelCredential.source == "direct", ModelCredential.ciphertext.is_not(None))
                           .order_by(ModelCredential.model_config_id, ModelCredential.version).with_for_update()).all())
    out: dict[str, Any] = {"current_key_version": ring.current, "rekeyed": 0, "already_current": 0, "unreadable": []}
    for cred in rows:
        if cred.key_version == ring.current:
            out["already_current"] += 1
            continue
        try:
            plain = decrypt(cred)
        except ProviderError as exc:
            out["unreadable"].append({"config_id": str(cred.model_config_id), "version": cred.version, "code": exc.code})
            continue
        sealed = crypto.encrypt(plain, config_id=str(cred.model_config_id), credential_version=cred.version, ring=ring)
        del plain
        cred.algorithm, cred.cipher_format, cred.key_version = sealed.algorithm, sealed.cipher_format, sealed.key_version
        cred.nonce, cred.ciphertext, cred.rekeyed_at = sealed.nonce, sealed.ciphertext, _now()
        out["rekeyed"] += 1
    db.flush()
    return out


def check(db: Session) -> dict[str, Any]:
    """Backup / restore check: which master keys are configured and whether every stored key decrypts."""
    ring = crypto.keyring()
    rows = list(db.scalars(select(ModelCredential).where(ModelCredential.source == "direct", ModelCredential.ciphertext.is_not(None))).all())
    by_key: dict[str, int] = {}
    failed: list[dict[str, Any]] = []
    for cred in rows:
        by_key[cred.key_version or "?"] = by_key.get(cred.key_version or "?", 0) + 1
        try:
            decrypt(cred)
        except ProviderError as exc:
            failed.append({"config_id": str(cred.model_config_id), "version": cred.version, "code": exc.code})
    return {"configured_keys": list(ring.keys), "current_key_version": ring.current, "keyring_error": ring.error,
            "stored_keys": len(rows), "by_master_key": by_key, "decrypt_failures": failed}


def _main(argv: list[str]) -> int:  # pragma: no cover - operator CLI
    from ..db import session_scope

    cmd = argv[1] if len(argv) > 1 else ""
    if cmd not in ("check", "rekey"):
        print("usage: python -m app.services.model_credentials check|rekey", file=sys.stderr)
        return 2
    try:
        with session_scope() as db:
            result = check(db) if cmd == "check" else rekey(db)
    except ProviderError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if not result.get("decrypt_failures") and not result.get("unreadable") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv))
