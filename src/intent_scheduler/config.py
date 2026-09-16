from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parents[2]


def load_environment() -> None:
    """Load local settings without overriding process-level configuration."""
    load_dotenv(PROJECT_DIR / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    provider: str = "mock"
    database_path: Path = PROJECT_DIR / "scheduler.db"
    capacity: int = 6
    intent_model: str = "gpt-5.6-luna"
    intent_fallback_model: str = "gpt-5.6-terra"
    candidate_model: str = "gpt-5.6-luna"
    candidate_fallback_model: str = "gpt-5.6-terra"
    judge_model: str = "gpt-5.6-luna"
    judge_fallback_model: str = "gpt-5.6-terra"
    scheduler_interval_seconds: float = 0.1

    @classmethod
    def from_env(cls) -> "Settings":
        load_environment()
        raw_db = os.getenv("INTENT_SCHEDULER_DB", "scheduler.db")
        db = Path(raw_db)
        if not db.is_absolute():
            db = PROJECT_DIR / db
        return cls(
            provider=os.getenv("INTENT_SCHEDULER_PROVIDER", "mock").lower(),
            database_path=db,
            capacity=int(os.getenv("INTENT_SCHEDULER_CAPACITY", "6")),
            intent_model=os.getenv("INTENT_SCHEDULER_INTENT_MODEL", "gpt-5.6-luna"),
            intent_fallback_model=os.getenv(
                "INTENT_SCHEDULER_INTENT_FALLBACK_MODEL", "gpt-5.6-terra"
            ),
            candidate_model=os.getenv(
                "INTENT_SCHEDULER_CANDIDATE_MODEL", "gpt-5.6-luna"
            ),
            candidate_fallback_model=os.getenv(
                "INTENT_SCHEDULER_CANDIDATE_FALLBACK_MODEL", "gpt-5.6-terra"
            ),
            judge_model=os.getenv("INTENT_SCHEDULER_JUDGE_MODEL", "gpt-5.6-luna"),
            judge_fallback_model=os.getenv(
                "INTENT_SCHEDULER_JUDGE_FALLBACK_MODEL", "gpt-5.6-terra"
            ),
            scheduler_interval_seconds=float(
                os.getenv("INTENT_SCHEDULER_INTERVAL_SECONDS", "0.1")
            ),
        )
