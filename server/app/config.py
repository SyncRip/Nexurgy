"""Runtime configuration. Everything comes from environment variables (or server/.env)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=SERVER_DIR / ".env",
        env_prefix="PARTROUTE_",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+psycopg://partroute:partroute@localhost:5544/partroute"

    # Deployment switches: API and workers must restart with the same values.
    # services.feature_flags owns effective dependencies and continuation rules.
    feature_collaboration: bool = False
    feature_tasks: bool = False
    feature_stages: bool = False
    feature_process_modules: bool = False
    feature_template_library: bool = False
    feature_turbo_choice: bool = False
    feature_model_choice: bool = False

    # Comma-separated list of allowed browser origins for CORS.
    cors_origins: str = "http://localhost:5188,http://127.0.0.1:5188,http://localhost:5173"

    storage_backend: Literal["local", "gcs"] = "local"
    storage_dir: Path = SERVER_DIR / "var" / "blobs"
    gcs_bucket: str | None = None
    max_upload_mb: int = 50

    token_ttl_hours: int = 24 * 7

    # Run workflow workers inside the API process (dev / single-service deploys).
    inline_worker: bool = True
    worker_threads: int = 2
    # P1 (execution-layer design §6.6): agent attempts are claimed only by these dedicated threads, apart from the
    # workflow threads (a multi-step agent holds its thread for minutes). 0 = this process runs no agent attempts.
    agent_threads: int = Field(default=1, ge=0, le=16)
    # P4 (design §9): the AI workspace ask runs on the read-only tool agent when its model can call tools ("auto");
    # "native" = the rollback: always the single structured call.
    assistant_runtime: Literal["auto", "native"] = "auto"
    # Ordinary auto/ask requests use the main Agent. Keep these rollback flags through the release observation period.
    # False stops the main Agent; new auto requests use read-only ask without classification or choice cards.
    # assistant_runtime=native also turns the read-only ask tool agent off, retaining the single structured call.
    assistant_orchestrator: bool = True
    # Main-agent turns are claimed only by these dedicated threads (turns of different conversations run side by side; one
    # more thread follows the sub-agents); 0 = none here.
    assistant_threads: int = Field(default=2, ge=0, le=4)
    worker_poll_seconds: float = 0.5
    node_lease_seconds: int = 300
    # A running node's heartbeat extends its lease, but never past this many seconds after it started
    # (a hung handler is reclaimed instead of holding the node forever).
    node_max_runtime_seconds: int = 3600

    # Seed model configs reference these env vars; keys themselves never enter the DB.
    default_gemini_model: str = "gemini-2.5-flash"
    default_openai_model: str = "gpt-5-mini"

    cad_tolerance: float = Field(default=0.05, gt=0)

    # Native DWG (app/cad/dwg.py, LibreDWG CLI run as separate processes).
    # None = look the tools up on PATH (the server image installs them in
    # /opt/libredwg/bin). dwg_timeout_s bounds one whole read or write.
    libredwg_bin_dir: Path | None = None
    dwg_timeout_s: float = Field(default=120.0, gt=0)

    # Deterministic fake model when no real key is configured (dev / tests only).
    # Results are labelled simulated everywhere and never count as real runs.
    ai_simulation: bool = False
    # Explicit compatibility exceptions for saved model configs. Restart API/workers together.
    # Other OpenAI-compatible configs keep strict structured output; replies are always validated.
    ai_compatible_non_strict_config_ids: list[str] = Field(default_factory=list)
    # Generation reliability J (app/ai/retry.py): first wait before retrying a transient model failure (doubled per
    # attempt, at most 20 s; a provider's Retry-After wins when longer). Tests set 0.
    ai_retry_backoff_s: float = Field(default=2.0, ge=0, le=20)

    # Quote dates are business dates in the shop's timezone, not UTC dates.
    business_timezone: str = "Asia/Shanghai"

    # Deployed demo (GCP): `python -m app.seed` refuses PARTROUTE_ENV=prod unless demo_seed is set, and
    # then only with seed_password (from Secret Manager; never the public development password).
    demo_seed: bool = False
    seed_password: str | None = None

    # Task acceptance: a submitter may not accept their own submission unless
    # this is set (demo environments only; PARTROUTE_ALLOW_SELF_REVIEW=true).
    allow_self_review: bool = False

    @model_validator(mode="after")
    def validate_runtime_environment(self) -> Settings:
        if self.env == "prod" and self.ai_simulation:
            raise ValueError("simulation_forbidden: PARTROUTE_AI_SIMULATION must be false in production")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    # Provider keys (GEMINI_API_KEY, OPENAI_API_KEY …) are read from the process
    # environment by secret_ref, so load server/.env into it without overriding
    # variables that are already set (Cloud Run / Secret Manager wins).
    from dotenv import load_dotenv

    load_dotenv(SERVER_DIR / ".env", override=False)
    return Settings()
