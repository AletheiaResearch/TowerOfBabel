"""Shared data types and constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class StepStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    ABSENT = "absent"  # endpoint had no artifact to download — terminal success
    FAILED = "failed"
    SKIPPED = "skipped"


PROCESS_FILE_KINDS: list[str] = [
    "shape-generation",
    "texture",
    "collision-preview",
    "physics-predictions",
    "validation-playback",
]

STEP_NAMES: list[str] = ["detail", *PROCESS_FILE_KINDS, "embedded-physics", "validation-report"]


@dataclass
class StepOutcome:
    status: StepStatus
    keys: dict[str, str] = field(default_factory=dict)
    http_status: int | None = None
    state: str | None = None
    bytes: int = 0
    error: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "keys": dict(self.keys),
            "http_status": self.http_status,
            "state": self.state,
            "bytes": self.bytes,
            "error": self.error,
            "completed_at": self.completed_at,
        }
