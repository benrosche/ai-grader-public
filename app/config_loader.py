from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig, Student, TaskConfig
from .settings import Settings


TASK_METADATA = "task.yml"
CONVERSATION_GUIDANCE = "conversation-guidance.md"
GRADING_GUIDANCE = "grading-guidance.md"
STUDENT_CODES = "student_codes.csv"

TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass
class ConfigLoader:
    settings: Settings

    async def load(self) -> AppConfig:
        return self._load_from_local()

    def _load_from_local(self) -> AppConfig:
        data_dir = self.settings.data_dir
        if not data_dir.exists():
            raise RuntimeError(f"Missing data folder: {data_dir}")

        tasks: dict[str, TaskConfig] = {}
        students_by_code: dict[str, tuple[TaskConfig, Student]] = {}
        duplicate_codes: set[str] = set()

        for task_dir in sorted(path for path in data_dir.iterdir() if path.is_dir()):
            if not (task_dir / TASK_METADATA).is_file():
                continue
            task = self._read_task(task_dir)
            tasks[task.task_id] = task
            for code, student in task.students.items():
                if code in students_by_code:
                    duplicate_codes.add(code)
                students_by_code[code] = (task, student)

        if not tasks:
            raise RuntimeError(f"No task folders found under {data_dir}")
        if duplicate_codes:
            joined = ", ".join(sorted(duplicate_codes))
            raise RuntimeError(
                "Student codes must be unique across task folders. "
                f"Duplicate code(s): {joined}"
            )

        return AppConfig(
            tasks=tasks,
            students_by_code=students_by_code,
            source=str(data_dir),
        )

    def _read_task(self, task_dir: Path) -> TaskConfig:
        task_id = task_dir.name
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise RuntimeError(
                f"Task folder name must use lowercase letters, numbers, "
                f"hyphens, or underscores: {task_dir}"
            )

        metadata = self._read_yaml(task_dir / TASK_METADATA)
        title = _required_str(metadata, "title", task_dir)
        artifact_label = _optional_str(metadata, "artifact_label", "Artifact")
        upload_required = _optional_bool(metadata, "upload_required", True)
        allowed_uploads = _parse_allowed_uploads(metadata, task_dir)
        categories = _parse_categories(metadata, task_dir)

        conversation_guidance = self._read_required_text(
            task_dir / CONVERSATION_GUIDANCE
        )
        first_message = extract_first_message(conversation_guidance, task_dir)
        grading_guidance = self._read_required_text(task_dir / GRADING_GUIDANCE)
        students = parse_students(self._read_required_text(task_dir / STUDENT_CODES))
        if not students:
            raise RuntimeError(f"{task_dir / STUDENT_CODES} did not contain valid codes.")

        return TaskConfig(
            task_id=task_id,
            title=title,
            artifact_label=artifact_label,
            upload_required=upload_required,
            allowed_uploads=allowed_uploads,
            categories=categories,
            conversation_guidance=conversation_guidance.strip(),
            first_message=first_message,
            grading_guidance=grading_guidance.strip(),
            students=students,
            source=str(task_dir),
            min_chat_seconds=_optional_int_metadata(metadata, "min_chat_seconds"),
            min_student_replies=_optional_int_metadata(metadata, "min_student_replies"),
        )

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        text = self._read_required_text(path)
        payload = yaml.safe_load(text) or {}
        if not isinstance(payload, dict):
            raise RuntimeError(f"{path} must contain a YAML object.")
        return payload

    def _read_required_text(self, path: Path) -> str:
        if not path.exists():
            raise RuntimeError(f"Missing task config file: {path}")
        return path.read_text(encoding="utf-8")


def parse_students(csv_text: str) -> dict[str, Student]:
    reader = csv.DictReader(io.StringIO(csv_text))
    required = {"code", "student_name"}
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise RuntimeError(
            "student_codes.csv must contain columns: code, student_name"
        )

    students: dict[str, Student] = {}
    for row in reader:
        code = (row.get("code") or "").strip()
        name = (row.get("student_name") or "").strip()
        student_id = (row.get("student_id") or "").strip()
        if not code or not name:
            continue
        if code in students:
            raise RuntimeError(f"Duplicate code in student_codes.csv: {code}")
        attempt_limit = _optional_int(
            row,
            "attempt_limit",
            "tries_allowed",
            "max_attempts",
        )
        attempts_taken = _optional_int(row, "attempts_taken", "tries_taken") or 0
        if attempt_limit is not None and attempt_limit < 0:
            raise RuntimeError("student_codes.csv attempt limits must be 0 or greater.")
        if attempts_taken < 0:
            raise RuntimeError("student_codes.csv tries taken must be 0 or greater.")
        students[code] = Student(
            code=code,
            name=name,
            student_id=student_id,
            attempt_limit=attempt_limit,
            attempts_taken=attempts_taken,
        )
    return students


def extract_first_message(conversation_guidance: str, task_dir: Path) -> str:
    in_block = False
    lines: list[str] = []
    for line in conversation_guidance.splitlines():
        normalized = line.strip().lower()
        if normalized == "```first-message":
            in_block = True
            continue
        if in_block and normalized == "```":
            break
        if in_block:
            lines.append(line)

    first_message = "\n".join(lines).strip()
    if not first_message:
        raise RuntimeError(
            f"{task_dir / CONVERSATION_GUIDANCE} must include a "
            "```first-message fenced block."
        )
    return first_message


def _parse_allowed_uploads(metadata: dict[str, Any], task_dir: Path) -> dict[str, str]:
    raw_uploads = metadata.get("allowed_uploads")
    if raw_uploads is None:
        return {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }
    if not isinstance(raw_uploads, list) or not raw_uploads:
        raise RuntimeError(f"{task_dir / TASK_METADATA} allowed_uploads must be a list.")

    allowed_uploads: dict[str, str] = {}
    for item in raw_uploads:
        if not isinstance(item, dict):
            raise RuntimeError("Each allowed_uploads entry must be a YAML object.")
        extension = _required_str(item, "extension", task_dir).lower()
        mime_type = _required_str(item, "mime_type", task_dir)
        if not extension.startswith("."):
            extension = f".{extension}"
        allowed_uploads[extension] = mime_type
    return allowed_uploads


def _parse_categories(metadata: dict[str, Any], task_dir: Path) -> list[str]:
    raw_categories = metadata.get("categories")
    if not isinstance(raw_categories, list) or not raw_categories:
        raise RuntimeError(f"{task_dir / TASK_METADATA} categories must be a list.")

    categories = []
    for item in raw_categories:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = _required_str(item, "name", task_dir)
        else:
            raise RuntimeError("Each category must be a string or object with name.")
        if name:
            categories.append(name)

    if len(set(categories)) != len(categories):
        raise RuntimeError(f"{task_dir / TASK_METADATA} category names must be unique.")
    return categories


def _required_str(mapping: dict[str, Any], key: str, context: Path) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{context} must define a non-empty {key}.")
    return value.strip()


def _optional_str(mapping: dict[str, Any], key: str, default: str) -> str:
    value = mapping.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"task.yml {key} must be a non-empty string.")
    return value.strip()


def _optional_bool(mapping: dict[str, Any], key: str, default: bool) -> bool:
    value = mapping.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RuntimeError(f"task.yml {key} must be true or false.")
    return value


def _optional_int_metadata(mapping: dict[str, Any], key: str) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        raise RuntimeError(f"task.yml {key} must be an integer 0 or greater.")
    return value


def _optional_int(row: dict[str, str], *column_names: str) -> int | None:
    for column_name in column_names:
        value = (row.get(column_name) or "").strip()
        if not value:
            continue
        try:
            return int(value)
        except ValueError as exc:
            raise RuntimeError(
                f"student_codes.csv column {column_name} must be an integer."
            ) from exc
    return None
