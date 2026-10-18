from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..ai import limits
from ..ai.credentials import keyring
from ..ai.registry import config_status, select_config
from ..ai.providers.base import ProviderError
from ..config import get_settings
from ..db import get_db
from ..models import User
from ..security import current_user
from ..services import ai as ai_svc
from ..services import feature_flags as features

router = APIRouter(tags=["system"])


def _kernel_version() -> dict:
    try:
        from ..cad.kernel import KERNEL_VERSION  # imported lazily: build123d is slow to import

        return {"ok": True, "version": KERNEL_VERSION}
    except Exception as exc:  # pragma: no cover - reported to the UI instead of failing
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/system/capabilities")
def capabilities(_: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """What is actually connected right now, so the UI can label unconfigured services."""
    s = get_settings()
    db_ok = db.scalar(text("select 1")) == 1
    providers: dict[str, dict] = {}
    configs = ai_svc.all_configs(db)
    # The same offline status as the model list and every real call (services/ai.py resolver): environment
    # references and encrypted direct keys alike.
    for cfg in configs:
        if not cfg.enabled:
            continue
        status = config_status(cfg)
        entry = providers.setdefault(cfg.provider, {"configs": 0, "with_key": 0, "ready": 0})
        entry["configs"] += 1
        entry["with_key"] += int(status.key_present)
        entry["ready"] += int(status.ready)
    ready_for = {}
    for purpose in ai_svc.CORE_PURPOSES:
        try:
            select_config(configs, purpose=purpose)
            ready_for[purpose] = True
        except ProviderError:
            ready_for[purpose] = False
    return {
        "features": features.effective_features(s),
        "database": {"ok": db_ok},
        "storage": {"backend": s.storage_backend},
        "kernel": _kernel_version(),
        "ai": {"providers": providers, "ready_for": ready_for, "any_ready": any(ready_for.values()),
               # Whether direct API keys can be saved / used here (a master key is configured); never key ids.
               "direct_keys": {"available": keyring().available}},
        "worker": {"inline": s.inline_worker, "threads": s.worker_threads},
        # S2: every entry's input limit and the context budgets (ai/limits.py); the frontend's maxLength reads these.
        "limits": limits.public(),
    }
