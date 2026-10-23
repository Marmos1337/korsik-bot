import os
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def env(name: str, default: str = "") -> str:
    # an empty line in .env (NAME=) means "use the default", same as a missing one
    return os.getenv(name, "").strip() or default


def env_int(name: str, default: int | None) -> int | None:
    value = env(name)
    return int(value) if value else default


def env_bool(name: str) -> bool:
    return env(name).lower() in ("1", "true", "yes", "on")


cfg = SimpleNamespace(
    MAX_BOT_TOKEN=env("MAX_BOT_TOKEN"),
    MAX_API_BASE=env("MAX_API_BASE", "https://platform-api.max.ru").rstrip("/"),
    BOT_USERNAME=env("BOT_USERNAME"),
    ADMIN_PASSWORD=env("ADMIN_PASSWORD"),
    SECRET_KEY=env("SECRET_KEY"),
    DATA_DIR=env("DATA_DIR", "data"),
    DB_PATH=env("DB_PATH", "data/korsik.db"),
    SCENARIO_PATH=env("SCENARIO_PATH", "scenario/scenario.xlsx"),
    HOLIDAYS_PATH=env("HOLIDAYS_PATH", "scenario/holidays.txt"),
    OLLAMA_URL=env("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
    LLM_MODEL=env("LLM_MODEL", "qwen3:8b"),
    EMBED_MODEL=env("EMBED_MODEL", "bge-m3"),
    DEMO_MODE=env_bool("DEMO_MODE"),
    HR_CHAT_ID=env_int("HR_CHAT_ID", None),
    WEB_HOST=env("WEB_HOST", "127.0.0.1"),
    WEB_PORT=env_int("WEB_PORT", 8080),
    PROBATION_MONTHS=env_int("PROBATION_MONTHS", 3),
    DEFAULT_TZ=env("DEFAULT_TZ", "Europe/Moscow"),
    STOP_TOPICS=[
        t.strip().lower()
        for t in env("STOP_TOPICS", "зарплат,уволь,увольн,больничн,конфликт").split(",")
        if t.strip()
    ],
)
