from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is in requirements.txt
    load_dotenv = None


def _bootstrap_tasks_dir(data_dir: Path, seed_root: Path) -> None:
    """Seed each task's folder under `data_dir` from `seed_root` if missing.

    Tasks live on the persistent volume so admin edits survive deploys. On
    first boot a task's folder doesn't exist (or exists without task.yml,
    e.g. only `uploads/` from a half-bootstrapped state), so we seed it
    from the `config/tasks/<id>/` snapshot committed to git (kept in sync
    nightly by the Railway-volume CI workflow — the seed mirrors the latest
    volume state).
    """
    if not seed_root.is_dir():
        return
    for seed_task_dir in seed_root.iterdir():
        if not seed_task_dir.is_dir():
            continue
        target = data_dir / seed_task_dir.name
        if (target / "task.yml").exists():
            continue
        target.mkdir(parents=True, exist_ok=True)
        for child in seed_task_dir.iterdir():
            dest = target / child.name
            if dest.exists():
                continue
            if child.is_dir():
                shutil.copytree(child, dest)
            else:
                shutil.copy2(child, dest)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    app_root: Path
    data_dir: Path

    openai_api_key: str | None
    openai_chat_model: str
    openai_judge_model: str

    admin_password: str
    session_secret: str

    default_min_chat_seconds: int
    default_min_student_replies: int
    max_attempts_per_student: int
    max_upload_mb: int
    session_idle_timeout_seconds: int

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load_settings() -> Settings:
    app_root = Path(__file__).resolve().parents[1]
    if load_dotenv is not None:
        load_dotenv(app_root / ".env")

    data_dir = Path(os.getenv("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = app_root / data_dir

    # Seed lives at /app/config/tasks/ on Railway because the volume mounts
    # over /app/data/ and would hide a seed placed inside data/.
    _bootstrap_tasks_dir(data_dir, app_root / "config" / "tasks")

    return Settings(
        app_root=app_root,
        data_dir=data_dir,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_chat_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-5.4-mini"),
        openai_judge_model=os.getenv("OPENAI_JUDGE_MODEL", "gpt-5.5"),
        admin_password=os.getenv("ADMIN_PASSWORD", "change-this-password"),
        session_secret=os.getenv("SESSION_SECRET", "change-this-random-string"),
        default_min_chat_seconds=_int_env("DEFAULT_MIN_CHAT_SECONDS", 300),
        default_min_student_replies=_int_env("DEFAULT_MIN_STUDENT_REPLIES", 4),
        max_attempts_per_student=_int_env("MAX_ATTEMPTS_PER_STUDENT", 2),
        max_upload_mb=_int_env("MAX_UPLOAD_MB", 15),
        session_idle_timeout_seconds=_int_env("SESSION_IDLE_TIMEOUT_SECONDS", 1800),
    )
