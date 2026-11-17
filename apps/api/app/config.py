"""Application settings, loaded once from apps/api/.env.

Usage anywhere in the backend:
    from app.config import settings
    settings.LLM_MODEL

Never read os.environ directly elsewhere; add a field here instead.
"""

from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# This file is apps/api/app/config.py, so parents[1] is apps/api.
# Resolving from __file__ means .env is found no matter which folder you run from.
API_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = API_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",  # .env may also hold NEXT_PUBLIC_* lines; ignore them
    )

    # --- App ---
    APP_ENV: str = "development"
    COLLEGE_NAME: str = "Government College of Engineering, Kolhapur"

    # --- Agent mode ---
    # auto   = use the local Ollama model if it is running, otherwise rule mode
    # ollama = local LLM only (chat shows an error if Ollama is down)
    # rules  = deterministic rule-based engine only (no LLM at all)
    LLM_PROVIDER: str = "auto"

    # --- Local LLM via Ollama (free, runs on this laptop) ---
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen2.5:3b"
    OLLAMA_TIMEOUT: float = 120.0

    # --- Anthropic (Phase 1 hello_agent.py only; optional, needs paid credits) ---
    ANTHROPIC_API_KEY: str = ""
    LLM_MODEL: str = "claude-haiku-5-5"

    # --- Embeddings (optional; matching renormalises weights without them) ---
    EMBEDDING_PROVIDER: str = "ollama"  # ollama | none
    OLLAMA_EMBED_MODEL: str = "nomic-embed-text"
    EMBEDDING_DIM: int = 768  # must match vector(768) in db/schema.sql
    OPENAI_API_KEY: str = ""  # unused in the no-paid-API build
    EMBEDDING_MODEL: str = "text-embedding-3-small"  # unused in the no-paid-API build

    # --- Database + Auth ---
    DATABASE_URL: str = ""
    SUPABASE_URL: str = ""
    SUPABASE_PUBLISHABLE_KEY: str = ""  # sb_publishable_... (public; used by the web page login)
    SUPABASE_SECRET_KEY: str = ""

    # --- HTTP ---
    ALLOWED_ORIGINS: str = "http://localhost:3000"  # comma-separated

    # --- Agent limits ---
    AGENT_MAX_STEPS: int = 6
    CHAT_HISTORY_LIMIT: int = 20

    # --- Local LLM protection ---
    OLLAMA_MAX_CONCURRENT: int = 1       # a laptop CPU can only run one model request well at a time
    OLLAMA_QUEUE_TIMEOUT: float = 45.0   # wait this long for a free slot, then use the rule engine
    OLLAMA_KEEP_ALIVE: str = "30m"       # keep the model loaded between messages (faster replies)

    # --- Rate limits ("<count>/<second|minute|hour|day>", several separated by ";") ---
    # In-memory, per server process: fine for one Render instance, NOT shared across workers/servers.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_CHAT: str = "10/minute;200/day"       # per user
    RATE_LIMIT_REPORTS: str = "5/minute;50/day"      # per user (lost/found forms)
    RATE_LIMIT_SEARCH: str = "30/minute"             # per user (search + matching)
    RATE_LIMIT_ADMIN: str = "60/minute"              # per admin (verify/reject/return)
    RATE_LIMIT_GLOBAL_IP: str = "120/minute"         # per client IP, every /api route except health

    # --- Request protection ---
    MAX_REQUEST_BYTES: int = 64_000      # bigger bodies get HTTP 413
    DB_CONNECT_TIMEOUT: int = 10         # seconds to open a database connection
    DB_POOL_TIMEOUT: float = 15.0        # seconds to wait for a free pooled connection
    DB_STATEMENT_TIMEOUT_MS: int = 15000 # any single SQL statement is cancelled after this

    @field_validator("*", mode="before")
    @classmethod
    def _comment_means_empty(cls, value: Any) -> Any:
        # In .env, `KEY=   # note` is read as the text "# note", not "".
        # Treat any value that starts with "#" as "not set".
        if isinstance(value, str) and value.strip().startswith("#"):
            return ""
        return value

    @property
    def allowed_origins_list(self) -> list[str]:
        """ALLOWED_ORIGINS split into a clean list (used for CORS in Phase 3)."""
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


settings = Settings()
