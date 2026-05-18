from __future__ import annotations

import csv
import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import GradingSession, JudgeResult, RESULT_FIELDS, TaskConfig

try:
    import fcntl
except ImportError:  # pragma: no cover - Railway/Linux and WSL have fcntl
    fcntl = None


@contextmanager
def _locked_file(path: Path, mode: str) -> Iterator:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, newline="", encoding="utf-8") as handle:
        if fcntl is not None:
            lock_type = fcntl.LOCK_EX if any(flag in mode for flag in "wa+") else fcntl.LOCK_SH
            fcntl.flock(handle.fileno(), lock_type)
        try:
            yield handle
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class ResultStorage:
    data_dir: Path

    def task_dir(self, task: TaskConfig) -> Path:
        return self.data_dir / task.task_id

    def results_csv(self, task: TaskConfig) -> Path:
        return self.task_dir(task) / "ai_marks.csv"

    def conversations_dir(self, task: TaskConfig) -> Path:
        return self.task_dir(task) / "conversations"

    def uploads_dir(self, task: TaskConfig) -> Path:
        return self.task_dir(task) / "uploads"

    @staticmethod
    def artifact_basename(
        student_code: str,
        attempt_number: int,
        timestamp: datetime,
        session_id: str,
    ) -> str:
        safe_code = _safe_filename(student_code) or "student"
        return (
            f"{safe_code}_attempt-{attempt_number}_"
            f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{session_id[:8]}"
        )

    def completed_results(self, task: TaskConfig) -> list[dict[str, str]]:
        results_csv = self.results_csv(task)
        if not results_csv.exists():
            return []
        self._ensure_result_schema(results_csv)
        with _locked_file(results_csv, "r") as handle:
            reader = csv.DictReader(handle)
            rows = [
                _normalize_result_row(row)
                for row in reader
            ]
        return sorted(
            rows,
            key=lambda row: row.get("timestamp", ""),
            reverse=True,
        )

    def ensure_file(self, task: TaskConfig) -> None:
        results_csv = self.results_csv(task)
        if results_csv.exists() and os.path.getsize(results_csv) > 0:
            self._ensure_result_schema(results_csv)
            return
        results_csv.parent.mkdir(parents=True, exist_ok=True)
        with _locked_file(results_csv, "w") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
            writer.writeheader()

    def completed_count(self, task: TaskConfig | None = None) -> int:
        csv_files = [self.results_csv(task)] if task else self.data_dir.glob("*/ai_marks.csv")
        sessions: set[tuple[Path, str]] = set()
        for results_csv in csv_files:
            if not results_csv.exists():
                continue
            with _locked_file(results_csv, "r") as handle:
                for row in csv.DictReader(handle):
                    sid = (row.get("session_id") or "").strip()
                    if sid:
                        sessions.add((results_csv, sid))
        return len(sessions)

    def attempts_for_code(self, task: TaskConfig, student_code: str) -> int:
        results_csv = self.results_csv(task)
        if not results_csv.exists():
            return 0
        sessions: set[str] = set()
        with _locked_file(results_csv, "r") as handle:
            for row in csv.DictReader(handle):
                if (row.get("student_code") or "").strip() == student_code:
                    sid = (row.get("session_id") or "").strip()
                    if sid:
                        sessions.add(sid)
        return len(sessions)

    def save_conversation(
        self,
        session: GradingSession,
        result: JudgeResult | None = None,
    ) -> Path:
        conversations_dir = self.conversations_dir(session.task)
        conversations_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc)
        basename = self.artifact_basename(
            session.student.code,
            session.attempt_number,
            timestamp,
            session.session_id,
        )
        path = conversations_dir / f"{basename}.json"
        payload = {
            "session_id": session.session_id,
            "task": {
                "id": session.task.task_id,
                "title": session.task.title,
                "categories": session.task.categories,
            },
            "attempt_number": session.attempt_number,
            "student": {
                "code": session.student.code,
                "name": session.student.name,
                "student_id": session.student.student_id,
            },
            "artifact_file": _relative_or_absolute(
                session.artifact_path,
                self.task_dir(session.task),
            ),
            "artifact_mime": session.artifact_mime,
            "started_at": session.started_at.isoformat(),
            "saved_at": timestamp.isoformat(),
            "completed": session.completed,
            "config_source": session.task.source,
            "transcript": [
                {
                    "role": turn.role,
                    "content": turn.content,
                    "timestamp": turn.timestamp.isoformat(),
                }
                for turn in session.transcript
            ],
            "judge_result": result.model_dump(mode="json") if result else None,
        }
        with _locked_file(path, "w") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return path

    def append_result(
        self,
        session: GradingSession,
        result: JudgeResult,
        conversation_path: Path | None = None,
    ) -> None:
        self.ensure_file(session.task)
        task_dir = self.task_dir(session.task)
        shared = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": session.task.task_id,
            "task_title": session.task.title,
            "session_id": session.session_id,
            "attempt_number": str(session.attempt_number),
            "student_code": session.student.code,
            "student_name": session.student.name,
            "student_id": session.student.student_id,
            "instructor_score": "",
            "review_status": result.review_status,
            "transcript_turns": str(len(session.transcript)),
            "transcript_json": json.dumps(
                [
                    {"role": turn.role, "content": turn.content}
                    for turn in session.transcript
                ],
                ensure_ascii=False,
            ),
            "conversation_file": _relative_or_absolute(conversation_path, task_dir),
            "artifact_file": _relative_or_absolute(session.artifact_path, task_dir),
            "config_source": session.task.source,
        }
        rows = []
        for category in result.categories:
            row = dict(shared)
            row["dimension_name"] = category.name
            row["ai_score"] = str(category.score_suggestion)
            row["ai_band"] = category.band
            row["ai_evidence"] = category.evidence
            row["ai_concerns"] = category.concerns
            rows.append(row)

        results_csv = self.results_csv(session.task)
        file_exists = results_csv.exists() and os.path.getsize(results_csv) > 0
        with _locked_file(results_csv, "a") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
            if not file_exists:
                writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def update_instructor_score(
        self,
        task: TaskConfig,
        session_id: str,
        dimension_name: str,
        score: str,
    ) -> bool:
        results_csv = self.results_csv(task)
        if not results_csv.exists():
            return False
        self._ensure_result_schema(results_csv)
        with _locked_file(results_csv, "r") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or list(RESULT_FIELDS)
            rows = list(reader)

        target_sid = session_id.strip()
        target_dim = dimension_name.strip()
        updated = False
        for row in rows:
            if (
                (row.get("session_id") or "").strip() == target_sid
                and (row.get("dimension_name") or "").strip() == target_dim
            ):
                row["instructor_score"] = score
                updated = True

        if not updated:
            return False

        with _locked_file(results_csv, "w") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
        return True

    def delete_session(self, task: TaskConfig, session_id: str) -> dict:
        results_csv = self.results_csv(task)
        if not results_csv.exists():
            return {"removed_rows": 0, "removed_files": []}
        self._ensure_result_schema(results_csv)
        with _locked_file(results_csv, "r") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or list(RESULT_FIELDS)
            rows = list(reader)

        target = session_id.strip()
        keep, drop = [], []
        for row in rows:
            (drop if (row.get("session_id") or "").strip() == target else keep).append(row)

        if not drop:
            return {"removed_rows": 0, "removed_files": []}

        task_dir = self.task_dir(task)
        files_to_remove: set[Path] = set()
        for row in drop:
            for field, expected in (
                ("conversation_file", self.conversations_dir(task)),
                ("artifact_file", self.uploads_dir(task)),
            ):
                rel = (row.get(field) or "").strip()
                if not rel:
                    continue
                candidate = (task_dir / rel).resolve()
                try:
                    candidate.relative_to(expected.resolve())
                except ValueError:
                    continue
                if candidate.is_file():
                    files_to_remove.add(candidate)

        with _locked_file(results_csv, "w") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in keep:
                writer.writerow({k: row.get(k, "") for k in fieldnames})

        removed_files = []
        for path in files_to_remove:
            try:
                path.unlink()
                removed_files.append(str(path.relative_to(task_dir)))
            except OSError:
                pass

        return {"removed_rows": len(drop), "removed_files": removed_files}

    def delete_task_data(self, task: TaskConfig) -> dict:
        """Remove every result, conversation, and upload for this task.

        The task's config files (task.yml etc. inside the task folder) are
        untouched, so new sessions can still start. Returns a summary of
        what was removed.
        """
        task_dir = self.task_dir(task)
        removed = {"csv": False, "conversations": 0, "uploads": 0}

        results_csv = self.results_csv(task)
        if results_csv.is_file():
            results_csv.unlink()
            removed["csv"] = True

        for sub_key, sub_dir in (
            ("conversations", self.conversations_dir(task)),
            ("uploads", self.uploads_dir(task)),
        ):
            if not sub_dir.is_dir():
                continue
            for child in sub_dir.iterdir():
                if child.is_file():
                    try:
                        child.unlink()
                        removed[sub_key] += 1
                    except OSError:
                        pass
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)

        if task_dir.is_dir() and not any(task_dir.iterdir()):
            try:
                task_dir.rmdir()
            except OSError:
                pass

        return removed

    def _ensure_result_schema(self, results_csv: Path) -> None:
        with _locked_file(results_csv, "r") as handle:
            reader = csv.DictReader(handle)
            existing_fields = reader.fieldnames or []
            sample = next(reader, None)
            if sample is not None and "categories_json" in existing_fields and not (
                sample.get("dimension_name") or ""
            ).strip():
                raise RuntimeError(
                    f"{results_csv} is in legacy wide format and cannot be read "
                    "by the current code. Convert it to the long layout (one row "
                    "per session × dimension) before continuing."
                )
            if all(field in existing_fields for field in RESULT_FIELDS):
                return
            rows = [sample] if sample is not None else []
            rows.extend(reader)

        with _locked_file(results_csv, "w") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(_normalize_result_row(row))


def _safe_filename(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in value.strip()
    )


def _relative_or_absolute(path: Path | None, base: Path) -> str:
    if path is None:
        return ""
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _normalize_result_row(row: dict[str, str]) -> dict[str, str]:
    normalized = {field: row.get(field, "") or "" for field in RESULT_FIELDS}
    if not normalized.get("review_status"):
        normalized["review_status"] = _review_status_from_legacy(
            row.get("confidence", ""),
            row.get("needs_manual_review", ""),
        )
    return normalized


def _review_status_from_legacy(confidence: str | None, review: str | None) -> str:
    normalized_confidence = (confidence or "").strip().lower()
    normalized_review = (review or "").strip().lower()
    if normalized_confidence == "low" or normalized_review == "true":
        return "Review (low confidence)"
    if normalized_confidence == "high" and normalized_review == "false":
        return "OK (high confidence)"
    if normalized_confidence == "medium" and normalized_review == "false":
        return "OK (medium confidence)"
    if normalized_confidence == "high":
        return "OK (high confidence)"
    if normalized_confidence == "medium":
        return "OK (medium confidence)"
    return ""
