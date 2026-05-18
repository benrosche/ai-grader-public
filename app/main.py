from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config_loader import ConfigLoader
from .models import ChatRequest, FinishRequest, GradingSession, TaskConfig, TranscriptTurn
from .openai_client import AIUnavailable, GradingAI
from .settings import load_settings
from .storage import ResultStorage


settings = load_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AI Grader")
templates = Jinja2Templates(directory=str(settings.app_root / "app" / "templates"))
app.mount(
    "/static",
    StaticFiles(directory=str(settings.app_root / "app" / "static")),
    name="static",
)

storage = ResultStorage(settings.data_dir)
config_loader = ConfigLoader(settings)
grading_ai = GradingAI(settings)
security = HTTPBasic()

_sessions: dict[str, GradingSession] = {}
_active_codes: set[str] = set()
_session_lock = threading.Lock()


def sweep_expired_sessions() -> None:
    timeout = settings.session_idle_timeout_seconds
    if timeout <= 0:
        return
    now = datetime.now(timezone.utc)
    artifact_paths: list[Path] = []
    with _session_lock:
        expired_ids = [
            sid
            for sid, session in _sessions.items()
            if not session.completed
            and (now - (session.last_activity_at or session.started_at)).total_seconds()
            > timeout
        ]
        for sid in expired_ids:
            session = _sessions.pop(sid, None)
            if session is not None:
                _active_codes.discard(session.student.code)
                if session.artifact_path is not None:
                    artifact_paths.append(session.artifact_path)
    for path in artifact_paths:
        path.unlink(missing_ok=True)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        context={
            "request": request,
            "min_chat_seconds": settings.default_min_chat_seconds,
            "min_student_replies": settings.default_min_student_replies,
            "max_attempts_per_student": settings.max_attempts_per_student,
            "max_upload_mb": settings.max_upload_mb,
        },
    )


@app.post("/api/start")
async def start_session(
    code: str = Form(...),
    artifact: UploadFile | None = File(None),
):
    normalized_code = code.strip()
    config = await config_loader.load()
    match = config.students_by_code.get(normalized_code)
    if match is None:
        raise HTTPException(status_code=404, detail="Unknown student code.")
    task, student = match

    sweep_expired_sessions()

    with _session_lock:
        if normalized_code in _active_codes:
            raise HTTPException(
                status_code=409,
                detail="This code already has an active grading session.",
            )

        attempts_taken = student.attempts_taken + storage.attempts_for_code(
            task,
            normalized_code,
        )
        attempt_limit = (
            student.attempt_limit
            if student.attempt_limit is not None
            else settings.max_attempts_per_student
        )
        if attempts_taken >= attempt_limit:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This code has already used all allowed conversation "
                    f"attempts ({attempt_limit})."
                ),
            )
        attempt_number = attempts_taken + 1
        _active_codes.add(normalized_code)

    session_id = secrets.token_urlsafe(24)
    artifact_path: Path | None = None
    try:
        artifact_path, artifact_mime = await save_upload(
            task,
            artifact,
            student_code=normalized_code,
            attempt_number=attempt_number,
            session_id=session_id,
        )
        session = GradingSession(
            session_id=session_id,
            attempt_number=attempt_number,
            task=task,
            student=student,
            artifact_path=artifact_path,
            artifact_mime=artifact_mime,
            started_at=datetime.now(timezone.utc),
        )
        session.openai_file_id = grading_ai.prepare_artifact_file(
            artifact_path,
            artifact_mime,
        )
        assistant_message = grading_ai.initial_question(session)
        session.transcript.append(
            TranscriptTurn(role="assistant", content=assistant_message)
        )
        with _session_lock:
            _sessions[session_id] = session
        min_chat_seconds = min_chat_seconds_for(task)
        min_student_replies = min_student_replies_for(task)
        return {
            "session_id": session_id,
            "student_name": student.name,
            "task_id": task.task_id,
            "task_title": task.title,
            "artifact_label": task.artifact_label,
            "assistant_message": assistant_message,
            "attempt_number": attempt_number,
            "attempt_limit": attempt_limit,
            "min_chat_seconds": min_chat_seconds,
            "min_student_replies": min_student_replies,
        }
    except HTTPException:
        cleanup_failed_start(normalized_code, artifact_path)
        raise
    except AIUnavailable as exc:
        cleanup_failed_start(normalized_code, artifact_path)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        cleanup_failed_start(normalized_code, artifact_path)
        raise HTTPException(
            status_code=500,
            detail="Could not start the grading conversation.",
        ) from exc


@app.post("/api/chat")
async def chat(request: ChatRequest):
    sweep_expired_sessions()
    session = get_session(request.session_id)
    if session.completed:
        return {"completed": True}

    session.touch()
    message = request.message.strip()
    session.transcript.append(TranscriptTurn(role="student", content=message))

    if session.can_finish(
        min_chat_seconds_for(session.task),
        min_student_replies_for(session.task),
    ):
        return finalize_session(session)

    try:
        assistant_message = grading_ai.next_question(session)
    except AIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    session.transcript.append(TranscriptTurn(role="assistant", content=assistant_message))
    return {
        "completed": False,
        "assistant_message": assistant_message,
        "elapsed_seconds": session.elapsed_seconds,
        "student_replies": session.student_reply_count,
        "can_finish": session.can_finish(
            min_chat_seconds_for(session.task),
            min_student_replies_for(session.task),
        ),
    }


@app.post("/api/finish")
async def finish(request: FinishRequest):
    sweep_expired_sessions()
    session = get_session(request.session_id)
    if session.completed:
        return {"completed": True}
    min_chat_seconds = min_chat_seconds_for(session.task)
    min_student_replies = min_student_replies_for(session.task)
    if not session.can_finish(min_chat_seconds, min_student_replies):
        raise HTTPException(
            status_code=400,
            detail=(
                "The conversation is not ready to finish yet. "
                f"Required: {min_chat_seconds} seconds and "
                f"{min_student_replies} student replies."
            ),
    )
    return finalize_session(session)


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    valid_user = secrets.compare_digest(credentials.username, "admin")
    valid_password = secrets.compare_digest(
        credentials.password,
        settings.admin_password,
    )
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, _: None = Depends(require_admin)):
    config = await config_loader.load()
    sweep_expired_sessions()
    with _session_lock:
        active_sessions = [
            session for session in _sessions.values() if not session.completed
        ]
    task_rows = [
        {
            "task": task,
            "completed_count": storage.completed_count(task),
            "results_path": storage.results_csv(task),
            "csv_href": f"/admin/download/{task.task_id}",
            "analysis_href": f"/admin/analysis/{task.task_id}",
            "edit_task_href": f"/admin/task/{task.task_id}/edit",
            "instructor_grade_href": f"/admin/instructor-grade/{task.task_id}",
            "delete_task_href": f"/admin/delete-task-data/{task.task_id}",
            "active_count": sum(
                1 for session in active_sessions if session.task.task_id == task.task_id
            ),
            "session_rows": (
                [
                    active_admin_row(task, session)
                    for session in active_sessions
                    if session.task.task_id == task.task_id
                ]
                + completed_admin_sessions(task, storage.completed_results(task))
            ),
        }
        for task in config.tasks.values()
    ]
    return templates.TemplateResponse(
        request,
        "admin.html",
        context={
            "request": request,
            "completed_count": storage.completed_count(),
            "active_sessions": active_sessions,
            "task_rows": task_rows,
            "config_source": config.source,
        },
    )


def active_admin_row(task: TaskConfig, session: GradingSession) -> dict:
    return {
        "status": "Active",
        "timestamp": "",
        "student_name": session.student.name,
        "student_code": session.student.code,
        "attempt_number": str(session.attempt_number),
        "progress": f"{session.elapsed_seconds}s · {session.student_reply_count} replies",
        "review_status": "",
        "conversation_link": None,
        "upload_link": None,
        "session_href": None,
        "session_id": session.session_id,
        "dimensions": [
            {
                "name": dim,
                "ai_score": "",
                "ai_band": "",
                "instructor_score": "",
                "editable": False,
            }
            for dim in task.categories
        ],
        "delete_href": None,
    }


def completed_admin_sessions(
    task: TaskConfig,
    rows: list[dict[str, str]],
) -> list[dict]:
    by_session: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for row in rows:
        sid = (row.get("session_id") or "").strip()
        if not sid:
            continue
        if sid not in by_session:
            by_session[sid] = []
            order.append(sid)
        by_session[sid].append(row)

    sessions = []
    for sid in order:
        group = by_session[sid]
        first = group[0]
        conversation_link = admin_reference_link(
            task,
            first.get("conversation_file", ""),
            storage.conversations_dir(task),
            "conversation",
        )
        session_href = (
            f"/admin/session/{task.task_id}/{conversation_link['name']}"
            if conversation_link
            else None
        )
        by_dim = {(r.get("dimension_name") or "").strip(): r for r in group}
        dimensions = []
        for dim in task.categories:
            row = by_dim.get(dim, {})
            ai_score = (row.get("ai_score") or "").strip()
            ai_band = (row.get("ai_band") or "").strip()
            dimensions.append(
                {
                    "name": dim,
                    "ai_score": ai_score,
                    "ai_band": ai_band,
                    "instructor_score": (row.get("instructor_score") or "").strip(),
                    "editable": True,
                }
            )

        sessions.append(
            {
                "status": "Completed",
                "timestamp": format_timestamp(first.get("timestamp", "")),
                "student_name": first.get("student_name", ""),
                "student_code": first.get("student_code", ""),
                "attempt_number": first.get("attempt_number", ""),
                "progress": f"{first.get('transcript_turns', '')} turns",
                "review_status": first.get("review_status", ""),
                "conversation_link": conversation_link,
                "upload_link": admin_reference_link(
                    task,
                    first.get("artifact_file", ""),
                    storage.uploads_dir(task),
                    "upload",
                ),
                "session_href": session_href,
                "session_id": sid,
                "dimensions": dimensions,
                "delete_href": f"/admin/delete-session/{task.task_id}/{sid}",
            }
        )
    return sessions


def admin_reference_link(
    task: TaskConfig,
    relative_value: str,
    expected_dir: Path,
    route_kind: str,
) -> dict[str, str] | None:
    path = referenced_admin_file(task, relative_value, expected_dir)
    if path is None:
        return None
    return {
        "name": path.name,
        "href": f"/admin/download/{task.task_id}/{route_kind}/{path.name}",
    }


def referenced_admin_file(
    task: TaskConfig,
    relative_value: str,
    expected_dir: Path,
) -> Path | None:
    value = (relative_value or "").strip()
    if not value:
        return None
    relative_path = Path(value)
    if relative_path.is_absolute():
        return None
    task_dir = storage.task_dir(task).resolve()
    expected = expected_dir.resolve()
    path = (task_dir / relative_path).resolve()
    try:
        path.relative_to(expected)
    except ValueError:
        return None
    if path.parent != expected or not path.is_file():
        return None
    return path


def format_timestamp(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="minutes")
        .replace("+00:00", "Z")
    )


@app.get("/admin/download/{task_id}")
async def download_results(task_id: str, _: None = Depends(require_admin)):
    config = await config_loader.load()
    task = config.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Unknown task.")
    storage.ensure_file(task)
    return FileResponse(
        storage.results_csv(task),
        media_type="text/csv",
        filename=f"{task.task_id}_ai_marks.csv",
    )


@app.get("/admin/session/{task_id}/{filename}", response_class=HTMLResponse)
async def admin_session_view(
    request: Request,
    task_id: str,
    filename: str,
    _: None = Depends(require_admin),
):
    task = await get_admin_task(task_id)
    conv_path = admin_data_file(storage.conversations_dir(task), filename)
    data = json.loads(conv_path.read_text(encoding="utf-8"))

    transcript = [
        {
            "role": turn.get("role", ""),
            "content": turn.get("content", ""),
            "timestamp": format_timestamp(turn.get("timestamp", "")),
        }
        for turn in data.get("transcript", [])
    ]

    upload_href = None
    upload_download_href = None
    upload_mime = data.get("artifact_mime") or ""
    artifact_file = (data.get("artifact_file") or "").strip()
    if artifact_file:
        upload_name = Path(artifact_file).name
        upload_path = storage.uploads_dir(task) / upload_name
        if upload_path.is_file():
            upload_href = f"/admin/view/{task.task_id}/upload/{upload_name}"
            upload_download_href = f"/admin/download/{task.task_id}/upload/{upload_name}"

    return templates.TemplateResponse(
        request,
        "admin_session.html",
        context={
            "request": request,
            "task": task,
            "student": data.get("student", {}),
            "attempt_number": data.get("attempt_number"),
            "started_at": format_timestamp(data.get("started_at", "")),
            "saved_at": format_timestamp(data.get("saved_at", "")),
            "transcript": transcript,
            "upload_href": upload_href,
            "upload_download_href": upload_download_href,
            "upload_mime": upload_mime,
            "judge_result": data.get("judge_result"),
        },
    )


@app.get("/admin/download/{task_id}/conversation/{filename}")
async def download_conversation(
    task_id: str,
    filename: str,
    _: None = Depends(require_admin),
):
    task = await get_admin_task(task_id)
    path = admin_data_file(storage.conversations_dir(task), filename)
    return FileResponse(path, media_type="application/json", filename=path.name)


@app.get("/admin/download/{task_id}/upload/{filename}")
async def download_upload(
    task_id: str,
    filename: str,
    _: None = Depends(require_admin),
):
    task = await get_admin_task(task_id)
    path = admin_data_file(storage.uploads_dir(task), filename)
    return FileResponse(path, filename=path.name)


@app.get("/admin/view/{task_id}/upload/{filename}")
async def view_upload(
    task_id: str,
    filename: str,
    _: None = Depends(require_admin),
):
    task = await get_admin_task(task_id)
    path = admin_data_file(storage.uploads_dir(task), filename)
    return FileResponse(path, content_disposition_type="inline")


@app.post("/admin/instructor-grade/{task_id}")
async def admin_set_instructor_grade(
    task_id: str,
    payload: dict,
    _: None = Depends(require_admin),
):
    task = await get_admin_task(task_id)
    session_id = str(payload.get("session_id") or "").strip()
    dimension = str(payload.get("dimension") or "").strip()
    raw_score = payload.get("score")
    if not session_id or not dimension:
        raise HTTPException(status_code=400, detail="session_id and dimension required.")
    if dimension not in task.categories:
        raise HTTPException(status_code=400, detail="Unknown dimension for this task.")

    score_str: str
    if raw_score is None or (isinstance(raw_score, str) and not raw_score.strip()):
        score_str = ""
    else:
        try:
            value = float(raw_score)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Score must be numeric.")
        if not 0 <= value <= 100:
            raise HTTPException(status_code=400, detail="Score must be between 0 and 100.")
        score_str = str(int(value)) if value.is_integer() else f"{value:g}"

    updated = storage.update_instructor_score(task, session_id, dimension, score_str)
    if not updated:
        raise HTTPException(status_code=404, detail="No matching session/dimension row.")
    return {"ok": True, "value": score_str}


TASK_FILES = {
    "task_yml": "task.yml",
    "conversation_guidance": "conversation-guidance.md",
    "grading_guidance": "grading-guidance.md",
    "student_codes": "student_codes.csv",
}


@app.get("/admin/task/new", response_class=HTMLResponse)
async def admin_new_task_form(request: Request, _: None = Depends(require_admin)):
    return templates.TemplateResponse(
        request,
        "admin_task_edit.html",
        context={
            "request": request,
            "mode": "new",
            "task_id": "",
            "files": {key: _new_task_template(key) for key in TASK_FILES},
            "save_href": "/admin/task/save",
            "error": None,
        },
    )


@app.get("/admin/task/{task_id}/edit", response_class=HTMLResponse)
async def admin_edit_task_form(
    request: Request,
    task_id: str,
    _: None = Depends(require_admin),
):
    task = await get_admin_task(task_id)
    task_dir = settings.data_dir / task.task_id
    files: dict[str, str] = {}
    for key, filename in TASK_FILES.items():
        path = task_dir / filename
        files[key] = path.read_text(encoding="utf-8") if path.is_file() else ""
    return templates.TemplateResponse(
        request,
        "admin_task_edit.html",
        context={
            "request": request,
            "mode": "edit",
            "task_id": task.task_id,
            "files": files,
            "save_href": "/admin/task/save",
            "error": None,
        },
    )


@app.post("/admin/task/save")
async def admin_save_task(payload: dict, _: None = Depends(require_admin)):
    from .config_loader import TASK_ID_PATTERN

    mode = (payload.get("mode") or "").strip()
    task_id = (payload.get("task_id") or "").strip()
    if mode not in {"new", "edit"}:
        raise HTTPException(status_code=400, detail="mode must be 'new' or 'edit'.")
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise HTTPException(
            status_code=400,
            detail="task_id must use lowercase letters, numbers, hyphens, or underscores.",
        )

    target_dir = settings.data_dir / task_id
    if mode == "new" and (target_dir / "task.yml").exists():
        raise HTTPException(status_code=409, detail=f"Task '{task_id}' already exists.")
    if mode == "edit" and not (target_dir / "task.yml").is_file():
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

    files = payload.get("files") or {}
    if not isinstance(files, dict):
        raise HTTPException(status_code=400, detail="files must be an object.")
    for key in TASK_FILES:
        if not isinstance(files.get(key), str):
            raise HTTPException(status_code=400, detail=f"files.{key} must be a string.")

    # Snapshot current contents so we can roll back if validation fails.
    backup: dict[str, str | None] = {}
    existed_before = target_dir.is_dir()
    if existed_before:
        for key, filename in TASK_FILES.items():
            path = target_dir / filename
            backup[key] = path.read_text(encoding="utf-8") if path.is_file() else None

    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        for key, filename in TASK_FILES.items():
            (target_dir / filename).write_text(files[key], encoding="utf-8")
        # Validate the task itself, then the whole config (catches cross-task
        # issues like duplicate student codes).
        config_loader._read_task(target_dir)
        config_loader._load_from_local()
    except Exception as exc:
        # Roll back to the prior contents (or remove the dir if it didn't exist).
        if existed_before:
            for key, filename in TASK_FILES.items():
                prior = backup.get(key)
                path = target_dir / filename
                if prior is None and path.exists():
                    path.unlink()
                elif prior is not None:
                    path.write_text(prior, encoding="utf-8")
        else:
            import shutil

            shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"ok": True, "task_id": task_id}


def _new_task_template(key: str) -> str:
    if key == "task_yml":
        return (
            "# Title shown to students and on the admin page.\n"
            "title: New task\n"
            "\n"
            "# Label for the upload field (e.g. \"Poster file\", \"Essay PDF\").\n"
            "artifact_label: Artifact\n"
            "\n"
            "# Set to false if the student should be able to start without an upload.\n"
            "upload_required: true\n"
            "\n"
            "# File types the student is allowed to upload. Add or remove rows as needed.\n"
            "allowed_uploads:\n"
            "  - extension: .pdf\n"
            "    mime_type: application/pdf\n"
            "\n"
            "# One entry per grading dimension. The AI returns a score and band for each.\n"
            "categories:\n"
            "  - Dimension one\n"
            "\n"
            "# Minimum chat duration and number of student replies before the student\n"
            "# is allowed to finish the conversation.\n"
            "min_chat_seconds: 300\n"
            "min_student_replies: 4\n"
        )
    if key == "conversation_guidance":
        return (
            "# Conversation Guidance\n"
            "\n"
            "## Scripted first message\n"
            "\n"
            "The text inside the fenced block below is sent verbatim as the assistant's first turn. Replace it but keep the fence intact.\n"
            "\n"
            "```first-message\n"
            "Hi, thanks for sharing your work. To start, could you tell me what you set out to do and what you'd like people to take away from it?\n"
            "```\n"
            "\n"
            "## Role and tone\n"
            "\n"
            "Describe the persona the AI should adopt (e.g. \"interested audience member at a poster session\", \"curious examiner\"). State the tone: friendly, concise, one question at a time. Tell the AI not to lecture, not to reveal grades or rubric placement, and to let the student do most of the explaining.\n"
            "\n"
            "## Conversation goal\n"
            "\n"
            "List the topics or sub-questions the AI should try to cover during the chat. Tell the AI to keep moving so every area gets a chance, even if some answers stay incomplete.\n"
            "\n"
            "## Pacing\n"
            "\n"
            "Cap the number of follow-ups per topic (e.g. one main question + at most one focused follow-up). Tell the AI to move on rather than over-probe.\n"
            "\n"
            "## When the conversation is finished\n"
            "\n"
            "Describe how the AI should close out: a short thank-you, no grading hints. The app appends its own final message after grading runs.\n"
        )
    if key == "grading_guidance":
        return (
            "# Grading Guidance\n"
            "\n"
            "Use only evidence from the uploaded artifact and the conversation transcript. Do not infer that the student did something well unless the artifact or conversation supports it.\n"
            "\n"
            "## Review status\n"
            "\n"
            "Return exactly one review status for the attempt:\n"
            "\n"
            "- `Review (low confidence)` — evidence is limited, ambiguous, or missing; flag for manual review.\n"
            "- `OK (medium confidence)` — evidence is adequate for an advisory grade but some uncertainty remains.\n"
            "- `OK (high confidence)` — the artifact and transcript provide strong, direct evidence.\n"
            "\n"
            "## Dimension one\n"
            "\n"
            "Describe what this dimension is evaluating in one or two sentences. Then describe each band:\n"
            "\n"
            "- **Exemplary (90-100)**: what a top-band response looks like.\n"
            "- **Proficient (80-89)**: clear and competent with minor gaps.\n"
            "- **Developing (70-79)**: uneven, partial, or unclear.\n"
            "- **Unacceptable (<70)**: missing, incorrect, or unintelligible.\n"
            "\n"
            "## Output expectations\n"
            "\n"
            "For each dimension, return a band (Exemplary / Proficient / Developing / Unacceptable), a numeric score 0-100, a short evidence line, and a short concerns line. Use the full range of bands and scores. Add one section per additional category in `task.yml`.\n"
        )
    if key == "student_codes":
        return "code,student_name,student_id\n"
    return ""


@app.post("/admin/delete-task-data/{task_id}")
async def admin_delete_task_data(
    task_id: str,
    _: None = Depends(require_admin),
):
    task = await get_admin_task(task_id)
    with _session_lock:
        active = [s for s in _sessions.values() if s.task.task_id == task_id and not s.completed]
    if active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(active)} active session(s) for this task. Wait for them "
                "to finish or expire before deleting task data."
            ),
        )
    summary = storage.delete_task_data(task)
    return {"ok": True, **summary}


@app.post("/admin/delete-session/{task_id}/{session_id}")
async def admin_delete_session(
    task_id: str,
    session_id: str,
    _: None = Depends(require_admin),
):
    task = await get_admin_task(task_id)
    summary = storage.delete_session(task, session_id)
    if summary["removed_rows"] == 0:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"ok": True, **summary}


@app.get("/admin/analysis/{task_id}", response_class=HTMLResponse)
async def admin_analysis(
    request: Request,
    task_id: str,
    _: None = Depends(require_admin),
):
    task = await get_admin_task(task_id)
    return templates.TemplateResponse(
        request,
        "admin_analysis.html",
        context=build_analysis_context(request, task),
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True}


async def get_admin_task(task_id: str) -> TaskConfig:
    config = await config_loader.load()
    task = config.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Unknown task.")
    return task


def build_analysis_context(request: Request, task: TaskConfig) -> dict:
    rows = storage.completed_results(task)
    # Pick the most recent (session_id, dimension) row per (student_code, dimension).
    # storage already sorts rows by timestamp desc, so the first occurrence wins.
    latest: dict[tuple[str, str], dict[str, str]] = {}
    transcripts_by_code: dict[str, list[dict[str, str]]] = {}
    transcript_seen: set[str] = set()
    for row in rows:
        code = (row.get("student_code") or "").strip()
        dim = (row.get("dimension_name") or "").strip()
        if not code or not dim:
            continue
        key = (code, dim)
        if key not in latest:
            latest[key] = row
        if code not in transcript_seen:
            transcript_seen.add(code)
            try:
                turns = json.loads(row.get("transcript_json") or "[]")
            except json.JSONDecodeError:
                turns = []
            transcripts_by_code[code] = turns if isinstance(turns, list) else []

    # Coverage table: every student × every dimension, in stable order.
    coverage = []
    students_sorted = sorted(
        task.students.values(),
        key=lambda s: (s.name or "").lower(),
    )
    per_dim: dict[str, list[dict[str, float]]] = {dim: [] for dim in task.categories}
    for student in students_sorted:
        for dim in task.categories:
            row = latest.get((student.code, dim))
            ai_score = _parse_score(row.get("ai_score") if row else "")
            instr_score = _parse_score(row.get("instructor_score") if row else "")
            diff = (
                ai_score - instr_score
                if ai_score is not None and instr_score is not None
                else None
            )
            coverage.append(
                {
                    "student_name": student.name,
                    "student_code": student.code,
                    "dimension": dim,
                    "ai_score": ai_score,
                    "instructor_score": instr_score,
                    "diff": diff,
                    "has_session": row is not None,
                }
            )
            if ai_score is not None and instr_score is not None:
                per_dim[dim].append(
                    {
                        "student": student.name,
                        "ai": ai_score,
                        "instructor": instr_score,
                    }
                )

    stats_per_dim = {dim: _agreement_stats(pairs) for dim, pairs in per_dim.items()}

    # Per-student blocks (only students who actually have a session).
    student_blocks = []
    for student in students_sorted:
        first_row = next(
            (latest.get((student.code, dim)) for dim in task.categories if latest.get((student.code, dim))),
            None,
        )
        if first_row is None:
            continue
        dims = []
        for dim in task.categories:
            row = latest.get((student.code, dim))
            if not row:
                continue
            dims.append(
                {
                    "name": dim,
                    "ai_score": _parse_score(row.get("ai_score")),
                    "ai_band": (row.get("ai_band") or "").strip(),
                    "ai_evidence": (row.get("ai_evidence") or "").strip(),
                    "ai_concerns": (row.get("ai_concerns") or "").strip(),
                    "instructor_score": _parse_score(row.get("instructor_score")),
                }
            )
        session_filename = ""
        conv_rel = (first_row.get("conversation_file") or "").strip()
        if conv_rel:
            session_filename = Path(conv_rel).name
        student_blocks.append(
            {
                "student_name": student.name,
                "student_code": student.code,
                "dimensions": dims,
                "transcript": transcripts_by_code.get(student.code, []),
                "session_href": (
                    f"/admin/session/{task.task_id}/{session_filename}"
                    if session_filename
                    else None
                ),
            }
        )

    chart_data = {
        dim: {
            "x": [p["instructor"] for p in pairs],
            "y": [p["ai"] for p in pairs],
            "labels": [p["student"] for p in pairs],
        }
        for dim, pairs in per_dim.items()
    }

    return {
        "request": request,
        "task": task,
        "coverage": coverage,
        "stats_per_dim": stats_per_dim,
        "student_blocks": student_blocks,
        "chart_data_json": json.dumps(chart_data, ensure_ascii=False),
        "total_students": len(task.students),
        "with_session": sum(
            1 for s in students_sorted if any(
                latest.get((s.code, dim)) for dim in task.categories
            )
        ),
    }


def _parse_score(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _agreement_stats(pairs: list[dict[str, float]]) -> dict:
    n = len(pairs)
    if n < 2:
        return {"n": n}
    xs = [p["instructor"] for p in pairs]
    ys = [p["ai"] for p in pairs]
    diffs = [y - x for x, y in zip(xs, ys)]

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    pearson = sxy / (sxx * syy) ** 0.5 if sxx > 0 and syy > 0 else None

    # Fisher z 95% CI
    ci_lo = ci_hi = None
    if pearson is not None and n > 3 and abs(pearson) < 1:
        import math

        z = math.atanh(pearson)
        se = 1.0 / math.sqrt(n - 3)
        ci_lo = math.tanh(z - 1.96 * se)
        ci_hi = math.tanh(z + 1.96 * se)

    # Spearman = Pearson on ranks
    spearman = _pearson_on_ranks(xs, ys)

    mean_diff = sum(diffs) / n
    mean_abs_diff = sum(abs(d) for d in diffs) / n

    return {
        "n": n,
        "pearson": pearson,
        "pearson_ci_lo": ci_lo,
        "pearson_ci_hi": ci_hi,
        "spearman": spearman,
        "mean_diff": mean_diff,
        "mean_abs_diff": mean_abs_diff,
    }


def _pearson_on_ranks(xs: list[float], ys: list[float]) -> float | None:
    rx = _ranks(xs)
    ry = _ranks(ys)
    n = len(rx)
    if n < 2:
        return None
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (
        sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)
    ) ** 0.5
    return num / den if den > 0 else None


def _ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda kv: kv[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def admin_data_file(directory: Path, filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Unknown file.")
    path = directory / filename
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown file.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Unknown file.")
    return path


def finalize_session(session: GradingSession) -> dict:
    marked_complete = False
    try:
        result = grading_ai.judge(session)
        session.completed = True
        marked_complete = True
        conversation_path = storage.save_conversation(session, result)
        storage.append_result(session, result, conversation_path)
    except AIUnavailable as exc:
        if marked_complete:
            session.completed = False
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        if marked_complete:
            session.completed = False
        raise HTTPException(status_code=500, detail="Could not save grading result.") from exc

    with _session_lock:
        _active_codes.discard(session.student.code)
    return {
        "completed": True,
        "result_saved": True,
    }


def get_session(session_id: str) -> GradingSession:
    with _session_lock:
        session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session.")
    return session


async def save_upload(
    task: TaskConfig,
    upload: UploadFile | None,
    *,
    student_code: str,
    attempt_number: int,
    session_id: str,
) -> tuple[Path | None, str | None]:
    if upload is None or not upload.filename:
        if task.upload_required:
            raise HTTPException(
                status_code=400,
                detail=f"{task.artifact_label} is required for this task.",
            )
        return None, None

    suffix = Path(upload.filename or "").suffix.lower()
    expected_mime = task.allowed_uploads.get(suffix)
    if expected_mime is None:
        allowed = ", ".join(sorted(task.allowed_uploads))
        raise HTTPException(
            status_code=400,
            detail=f"{task.artifact_label} must use one of these formats: {allowed}.",
        )

    content = await upload.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"{task.artifact_label} file is larger than {settings.max_upload_mb} MB.",
        )

    basename = storage.artifact_basename(
        student_code,
        attempt_number,
        datetime.now(timezone.utc),
        session_id,
    )
    filename = f"{basename}{suffix}"
    uploads_dir = storage.uploads_dir(task)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    path = uploads_dir / filename
    path.write_bytes(content)
    return path, expected_mime


def cleanup_failed_start(code: str, artifact_path: Path | None) -> None:
    with _session_lock:
        _active_codes.discard(code)
    if artifact_path and artifact_path.exists():
        artifact_path.unlink(missing_ok=True)


def min_chat_seconds_for(task: TaskConfig) -> int:
    return (
        task.min_chat_seconds
        if task.min_chat_seconds is not None
        else settings.default_min_chat_seconds
    )


def min_student_replies_for(task: TaskConfig) -> int:
    return (
        task.min_student_replies
        if task.min_student_replies is not None
        else settings.default_min_student_replies
    )
