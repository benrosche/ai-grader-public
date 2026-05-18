from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


Band = Literal["Exemplary", "Proficient", "Developing", "Unacceptable"]
ReviewStatus = Literal[
    "Review (low confidence)",
    "OK (medium confidence)",
    "OK (high confidence)",
]


@dataclass(frozen=True)
class Student:
    code: str
    name: str
    student_id: str = ""
    attempt_limit: int | None = None
    attempts_taken: int = 0


@dataclass(frozen=True)
class UploadRule:
    extension: str
    mime_type: str


@dataclass(frozen=True)
class TaskConfig:
    task_id: str
    title: str
    artifact_label: str
    upload_required: bool
    allowed_uploads: dict[str, str]
    categories: list[str]
    conversation_guidance: str
    first_message: str
    grading_guidance: str
    students: dict[str, Student]
    source: str
    min_chat_seconds: int | None = None
    min_student_replies: int | None = None


@dataclass(frozen=True)
class AppConfig:
    tasks: dict[str, TaskConfig]
    students_by_code: dict[str, tuple[TaskConfig, Student]]
    source: str


@dataclass
class TranscriptTurn:
    role: Literal["assistant", "student"]
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GradingSession:
    session_id: str
    attempt_number: int
    task: TaskConfig
    student: Student
    artifact_path: Path | None
    artifact_mime: str | None
    started_at: datetime
    openai_file_id: str | None = None
    transcript: list[TranscriptTurn] = field(default_factory=list)
    completed: bool = False
    last_activity_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.last_activity_at is None:
            self.last_activity_at = self.started_at

    def touch(self) -> None:
        self.last_activity_at = datetime.now(timezone.utc)

    @property
    def student_reply_count(self) -> int:
        return sum(1 for turn in self.transcript if turn.role == "student")

    @property
    def elapsed_seconds(self) -> int:
        delta = datetime.now(timezone.utc) - self.started_at
        return max(0, int(delta.total_seconds()))

    @property
    def idle_seconds(self) -> int:
        reference = self.last_activity_at or self.started_at
        delta = datetime.now(timezone.utc) - reference
        return max(0, int(delta.total_seconds()))

    def can_finish(self, min_seconds: int, min_replies: int) -> bool:
        return (
            self.elapsed_seconds >= min_seconds
            and self.student_reply_count >= min_replies
        )


class ChatRequest(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=4000)


class FinishRequest(BaseModel):
    session_id: str


class CategoryGrade(BaseModel):
    name: str = Field(max_length=120)
    band: Band
    score_suggestion: int = Field(ge=0, le=100)
    evidence: str = Field(max_length=1200)
    concerns: str = Field(max_length=1200)


class JudgeResult(BaseModel):
    categories: list[CategoryGrade]
    review_status: ReviewStatus

    @classmethod
    def json_schema_for_openai(cls, category_names: list[str]) -> dict:
        category_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "name",
                "band",
                "score_suggestion",
                "evidence",
                "concerns",
            ],
            "properties": {
                "name": {"type": "string", "enum": category_names},
                "band": {
                    "type": "string",
                    "enum": [
                        "Exemplary",
                        "Proficient",
                        "Developing",
                        "Unacceptable",
                    ],
                },
                "score_suggestion": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                },
                "evidence": {"type": "string"},
                "concerns": {"type": "string"},
            },
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "categories",
                "review_status",
            ],
            "properties": {
                "categories": {
                    "type": "array",
                    "minItems": len(category_names),
                    "maxItems": len(category_names),
                    "items": category_schema,
                },
                "review_status": {
                    "type": "string",
                    "enum": [
                        "Review (low confidence)",
                        "OK (medium confidence)",
                        "OK (high confidence)",
                    ],
                },
            },
        }


RESULT_FIELDS = [
    "timestamp",
    "task_id",
    "task_title",
    "session_id",
    "attempt_number",
    "student_code",
    "student_name",
    "student_id",
    "dimension_name",
    "ai_score",
    "ai_band",
    "ai_evidence",
    "ai_concerns",
    "instructor_score",
    "review_status",
    "transcript_turns",
    "transcript_json",
    "conversation_file",
    "artifact_file",
    "config_source",
]
