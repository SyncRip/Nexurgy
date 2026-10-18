"""Content-addressed blob storage. Local directory for dev, GCS in the cloud.

The database only stores sha256, size, content type and storage key (table
`blobs`); bytes live here. Switch with PARTROUTE_STORAGE_BACKEND=local|gcs.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from .config import get_settings
from .models import Blob


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / key

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class GcsBlobStore:
    """Google Cloud Storage backend (requires the `gcs` extra and ADC credentials)."""

    def __init__(self, bucket: str):
        from google.cloud import storage  # type: ignore[import-not-found]

        self.bucket = storage.Client().bucket(bucket)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        blob = self.bucket.blob(key)
        if not blob.exists():
            blob.upload_from_string(data, content_type=content_type)

    def get(self, key: str) -> bytes:
        return self.bucket.blob(key).download_as_bytes()

    def exists(self, key: str) -> bool:
        return self.bucket.blob(key).exists()


@lru_cache
def blob_store() -> BlobStore:
    s = get_settings()
    if s.storage_backend == "gcs":
        if not s.gcs_bucket:
            raise RuntimeError("PARTROUTE_GCS_BUCKET is required when storage_backend=gcs")
        return GcsBlobStore(s.gcs_bucket)
    return LocalBlobStore(Path(s.storage_dir))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_blob(db: Session, data: bytes, content_type: str) -> str:
    """Store bytes once and register them in `blobs`. Returns the sha256."""
    digest = sha256_hex(data)
    if db.get(Blob, digest) is None:
        blob_store().put(digest, data, content_type)
        db.add(Blob(sha256=digest, size=len(data), content_type=content_type, storage_key=digest))
        db.flush()
    return digest


def read_blob(db: Session, digest: str) -> tuple[bytes, Blob]:
    row = db.get(Blob, digest)
    if row is None:
        raise FileNotFoundError(digest)
    return blob_store().get(row.storage_key), row
