from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

from openai import OpenAI

from .models import GradingSession, JudgeResult, TranscriptTurn
from .settings import Settings


class AIUnavailable(RuntimeError):
    pass


class GradingAI:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = (
            OpenAI(api_key=settings.openai_api_key)
            if settings.openai_api_key
            else None
        )

    def _require_client(self) -> OpenAI:
        if self._client is None:
            raise AIUnavailable("OpenAI is not configured. Set OPENAI_API_KEY.")
        return self._client

    def prepare_artifact_file(
        self,
        artifact_path: Path | None,
        artifact_mime: str | None,
    ) -> str | None:
        if artifact_path is None or (artifact_mime or "").startswith("image/"):
            return None
        client = self._require_client()
        with artifact_path.open("rb") as handle:
            uploaded = client.files.create(file=handle, purpose="user_data")
        return uploaded.id

    def initial_question(self, session: GradingSession) -> str:
        return session.task.first_message

    def next_question(self, session: GradingSession) -> str:
        client = self._require_client()
        response = client.responses.create(
            model=self.settings.openai_chat_model,
            input=self._chat_input(
                session=session,
                task=(
                    "Continue the grading conversation. Ask exactly one concise, "
                    "natural question that responds to the student's most recent "
                    "answer and targets the most important missing or unclear "
                    "evidence. First acknowledge the substance of the student's "
                    "answer in a short phrase or sentence, then ask the next "
                    "question. Do not grade, score, reveal grade bands, or mention "
                    "rubric placement."
                ),
            ),
        )
        return _clean_output(response.output_text, session.task.title)

    def judge(self, session: GradingSession) -> JudgeResult:
        client = self._require_client()
        response = client.responses.create(
            model=self.settings.openai_judge_model,
            input=self._judge_input(session),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "grading_result",
                    "strict": True,
                    "schema": JudgeResult.json_schema_for_openai(
                        session.task.categories
                    ),
                }
            },
        )
        try:
            payload = json.loads(response.output_text)
            result = JudgeResult.model_validate(payload)
            _validate_categories(result, session.task.categories)
            return result
        except Exception as exc:
            raise RuntimeError("Rubric judge returned malformed grading JSON.") from exc

    def _chat_input(self, session: GradingSession, task: str) -> list[dict]:
        system = (
            "You are an interested, intellectually curious audience member or "
            "assessor for a student grading task. Follow the instructor "
            "instructions in conversation-guidance.md. Never reveal grades, "
            "scores, bands, hidden evaluation, or grading strategy to the "
            "student.\n\n"
            f"Task: {session.task.title} ({session.task.task_id})\n"
            f"Categories: {', '.join(session.task.categories)}\n\n"
            "<conversation-guidance.md>\n"
            f"{session.task.conversation_guidance}\n"
            "</conversation-guidance.md>"
        )
        user_text = (
            f"Student: {session.student.name}\n"
            f"Elapsed seconds: {session.elapsed_seconds}\n"
            f"Student replies so far: {session.student_reply_count}\n\n"
            "<transcript>\n"
            f"{format_transcript(session.transcript)}\n"
            "</transcript>\n\n"
            f"{task}"
        )
        return [
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {
                "role": "user",
                "content": self._artifact_blocks(session)
                + [{"type": "input_text", "text": user_text}],
            },
        ]

    def _judge_input(self, session: GradingSession) -> list[dict]:
        system = (
            "You are a careful grading assistant. Use only the submitted "
            "artifact and the transcript as evidence. Follow "
            "grading-guidance.md exactly. Return only JSON that conforms to the "
            "supplied schema.\n\n"
            f"Task: {session.task.title} ({session.task.task_id})\n"
            f"Categories to grade: {', '.join(session.task.categories)}\n\n"
            "<grading-guidance.md>\n"
            f"{session.task.grading_guidance}\n"
            "</grading-guidance.md>"
        )
        artifact_text = (
            "No artifact was submitted for this task."
            if session.artifact_path is None
            else f"Submitted artifact: {session.artifact_path.name}"
        )
        user_text = (
            f"Student: {session.student.name}\n"
            f"{artifact_text}\n"
            "Grade each configured category separately. Do not calculate an "
            "aggregate grade. If evidence is missing, say that directly in "
            "the category concerns and use Review (low confidence) as the "
            "overall review status.\n\n"
            "<transcript>\n"
            f"{format_transcript(session.transcript)}\n"
            "</transcript>"
        )
        return [
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {
                "role": "user",
                "content": self._artifact_blocks(session)
                + [{"type": "input_text", "text": user_text}],
            },
        ]

    def _artifact_blocks(self, session: GradingSession) -> list[dict]:
        if session.artifact_path is None:
            return []
        if (session.artifact_mime or "").startswith("image/"):
            data_url = artifact_to_data_url(
                session.artifact_path,
                session.artifact_mime,
            )
            return [{"type": "input_image", "image_url": data_url}]
        if not session.openai_file_id:
            raise AIUnavailable("Artifact file was not uploaded to OpenAI.")
        return [{"type": "input_file", "file_id": session.openai_file_id}]


def artifact_to_data_url(path: Path, mime_type: str | None = None) -> str:
    mime = mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def format_transcript(turns: list[TranscriptTurn]) -> str:
    if not turns:
        return "(no transcript yet)"
    lines = []
    for turn in turns:
        label = "Assistant" if turn.role == "assistant" else "Student"
        lines.append(f"{label}: {turn.content}")
    return "\n".join(lines)


def _validate_categories(result: JudgeResult, category_names: list[str]) -> None:
    expected = set(category_names)
    received = [category.name for category in result.categories]
    if set(received) != expected or len(received) != len(expected):
        raise RuntimeError(
            "Rubric judge returned categories that do not match the task config."
        )


def _clean_output(text: str | None, task_title: str) -> str:
    value = (text or "").strip()
    if not value:
        return f"Could you tell me more about your work for {task_title}?"
    return value
