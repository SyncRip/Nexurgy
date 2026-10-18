"""FastAPI application entry point: `uvicorn app.main:app`."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import (
    agent_executions,
    agent_profiles,
    ai,
    approval_requests,
    assistant,
    assemblies,
    assembly_drawings,
    assets,
    auth,
    collaboration,
    collaboration_actions,
    deliveries,
    drawing_documents,
    drawings,
    engineering_team,
    engineering_specs,
    execution_policy,
    files,
    knowledge,
    manufacturing,
    milestone_reviews,
    notifications,
    overview,
    parts,
    process_modules,
    projects,
    resources,
    revisions,
    system,
    tasks,
    template_library,
    todos,
    workflows,
)
from .config import get_settings
from .errors import install_handlers

log = logging.getLogger("partroute")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.validate_runtime_environment()
    from .db import session_scope
    from .services.feature_flags import record_rollout

    with session_scope() as db:
        record_rollout(db, source="api")
    worker = None
    if settings.inline_worker and settings.env != "test":
        try:
            from .worker import WorkerPool

            worker = WorkerPool(threads=settings.worker_threads, agent_threads=settings.agent_threads)
            worker.start()
        except ImportError:  # worker not wired yet
            log.warning("inline worker unavailable")
    yield
    if worker is not None:
        worker.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    settings.validate_runtime_environment()
    app = FastAPI(title="PartRoute API", version="0.4.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )
    install_handlers(app)

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict:
        return {"ok": True}

    for module in (auth, projects, parts, files, revisions, drawings, manufacturing, workflows, ai, assets, tasks, approval_requests, overview,
                   template_library, todos, milestone_reviews, process_modules, notifications,
                   agent_profiles, collaboration, collaboration_actions, resources, knowledge, assistant, system,
                   engineering_specs, engineering_team, assemblies, assembly_drawings, drawing_documents, deliveries, execution_policy, agent_executions):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


app = create_app()
