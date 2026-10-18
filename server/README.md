# PartRoute server (v0.4)

FastAPI + PostgreSQL + build123d (OpenCascade) backend. It owns every
authoritative computation: geometry builds, STEP/GLB/drawing generation,
process planning, quoting, approvals, workflow execution and model calls.
The browser only talks to this API.

## Run locally

Requirements: Docker (for PostgreSQL), [uv](https://docs.astral.sh/uv/), Python 3.12.

```bash
docker compose up -d db                 # from the repo root: PostgreSQL 16 on localhost:5544
cd server
uv sync                                 # Apple Silicon with an x86 uv: uv venv --python cpython-3.12-macos-aarch64-none first
uv run alembic upgrade head
uv run python -m app.seed               # dev users + DEMO project + model configs (accounts in app/seed.py)
uv run uvicorn app.main:app --port 8000 # API + inline workflow workers
```

Open http://localhost:8000/docs for the OpenAPI UI. Copy `.env.example` to
`.env` to change settings. Model keys go into `.env` (or the environment) as
`GEMINI_API_KEY` / `OPENAI_API_KEY`; model configs reference them by name and
the keys never enter the database. Without a key, AI nodes fail with
`missing_key`, and every non-AI feature (manual editor, import, drawings,
planning, quoting, workflows) still works.

`PARTROUTE_AI_SIMULATION=true` swaps in a deterministic fake model for local
demos; its results are marked `simulated` everywhere.

Run workers as a separate process instead of inside the API:

```bash
PARTROUTE_INLINE_WORKER=false uv run uvicorn app.main:app --port 8000
uv run python -m app.worker
```

Tests (need the compose database): `uv run pytest -q`.

## Layout

| Path | What |
|---|---|
| `app/api/` | HTTP routers (auth, projects, parts, files, revisions, drawings, manufacturing, workflows, ai, system; project management layer: tasks, assets, approval_requests, overview, template_library, milestone_reviews, process_modules, notifications) |
| `app/services/` | Business services shared by routers and workflow nodes |
| `app/cad/` | Geometry library: operation schema (`ops.py`), kernel build, imports, drawings, diff — no DB |
| `app/ai/` | Model providers (Gemini, OpenAI, OpenAI-compatible), modeling and drawing-intake agents — no DB |
| `app/domain/` | Deterministic planning and costing rules — no DB, no clock |
| `app/workflow/` | Node catalog, graph validation, run engine, handlers, templates |
| `alembic/` | Database migrations |

Contracts between these parts: `.trellis/tasks/archive/2026-10/10-02-v04-platform/design.md`. The project management layer (tasks, asset catalogue, deliverables, approval requests, template versions, stage reviews, process modules, notifications) follows `docs/archive/project-task-asset-design.zh-CN.md`; its implementation contracts are `.trellis/tasks/archive/2026-10/10-05-project-task-asset/design.md` (P0–P2) and `.trellis/tasks/archive/2026-10/10-05-templates-stages/design.md` (P3–P4). The worker also dispatches outbox events into in-app notifications.

## Deploy (Google Cloud)

Deployed as a demo on 2026-10-10 (Cloud Run + Cloud SQL, Tokyo); the step-by-step runbook with the exact commands is [docs/deploy-gcp.zh-CN.md](../docs/deploy-gcp.zh-CN.md). Summary:

1. **Database**: Cloud SQL for PostgreSQL 16. Set `PARTROUTE_DATABASE_URL`
   (Cloud SQL connector or private IP) from Secret Manager.
2. **Files**: a GCS bucket; set `PARTROUTE_STORAGE_BACKEND=gcs` and
   `PARTROUTE_GCS_BUCKET`. The service account needs object read/write.
3. **Image**: `docker build --platform linux/amd64 -t REGION-docker.pkg.dev/PROJECT/partroute/server .`
   then push to Artifact Registry.
4. **API service**: Cloud Run with the image; env `PARTROUTE_ENV=prod`,
   `PARTROUTE_CORS_ORIGINS=https://<frontend-host>`, keys from Secret Manager
   (`GEMINI_API_KEY`, optionally `OPENAI_API_KEY`). Run migrations once per
   release with a Cloud Run job: `alembic upgrade head`.
5. **Workers**: either keep `PARTROUTE_INLINE_WORKER=true` with CPU always
   allocated and min instances ≥ 1, or deploy the same image as a second
   service running `python -m app.worker` with `PARTROUTE_INLINE_WORKER=false`
   on the API.
6. **Rollback**: Cloud Run revisions roll back independently of the frontend;
   migrations are additive so the previous revision keeps working.

Memory: the OCCT kernel needs about 1 GiB; give the service 2 GiB.
