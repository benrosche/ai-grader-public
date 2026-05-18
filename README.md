# AI Grader

FastAPI app for AI-assisted grading conversations. Each student enters a per-task code, optionally uploads an artifact (PDF, image, etc.), has a short conversation with an AI that plays a domain-appropriate persona, and the app stores AI-suggested per-dimension grades plus the full transcript. Instructors review and overlay their own grades through an admin page.

The app supports any number of tasks, any number of grading dimensions per task, and runs on a single web service plus one persistent volume.

**New here?** Start with [TUTORIAL.md](TUTORIAL.md) — a narrative walkthrough that uses a small matrix-multiplication problem set as a worked example, takes you from a fresh clone to a deployed Railway instance, and explains each piece along the way.

## What you can do with it

- **Run multiple tasks** side by side, each with its own rubric, conversation guidance, and student roster.
- **Per-task admin page** with one row per completed session × grading dimension, inline instructor-grade entry (auto-saved as you type), and a side-by-side view of the conversation transcript and the student's uploaded artifact.
- **Per-task analysis page** rendered live by the server: coverage table over the whole student roster (so missing sessions are visible), AI-vs-instructor scatter and signed-diff plots, Pearson / Spearman / mean signed diff statistics, and per-student summary blocks with collapsible transcripts.
- **Create, edit, and delete tasks from the admin UI**: raw editor for the four files that make up a task, with validation on save.
- **CSV export** of grades and a downloadable JSON per session.
- **Nightly GitHub backup** of the volume to this repository.

## Architecture at a glance

```
Browser ──HTTPS──> FastAPI (app/) ──> OpenAI Responses API
                       │
                       └── Persistent volume (mounted at /app/data/ on Railway):
                              <task_id>/task.yml, *.md, student_codes.csv   ← task config
                              <task_id>/ai_marks.csv, conversations/, uploads/   ← session data
```

On Railway the persistent volume mounts at `/app/data/`, so the in-repo seed used to bootstrap a fresh volume lives at `/app/config/tasks/` (outside the mount). On first boot, for each `config/tasks/<id>/` the app copies its files into `<volume>/<id>/` (skipping any task whose folder already has a `task.yml`); from then on the volume is the canonical store for everything.

## Task config

Each task is a folder with four files. At runtime the app reads them from `data/<task_id>/` (the persistent volume); the same four files under `config/tasks/<task_id>/` are the seed copied in on first boot.

```text
data/<task_id>/             # live store (volume); seed under config/tasks/<task_id>/
  task.yml                  # title, upload rules, grading dimensions
  conversation-guidance.md  # AI persona, opening line, conversation goals
  grading-guidance.md       # rubric the AI uses to assign per-dimension grades
  student_codes.csv         # roster of codes that can start a session
```

Example `task.yml`:

```yaml
title: SNA Poster Presentation
artifact_label: Poster file
upload_required: true
allowed_uploads:
  - extension: .pdf
    mime_type: application/pdf
  - extension: .png
    mime_type: image/png
categories:
  - Poster
  - Presentation
min_chat_seconds: 300
min_student_replies: 10
```

`student_codes.csv`:

```csv
code,student_name,student_id
poster-001,Student Name,optional-id
```

Required columns: `code`, `student_name`. Optional: `student_id`, `attempt_limit` (aliases: `tries_allowed`, `max_attempts`), `attempts_taken` (alias: `tries_taken`). Codes must be unique across all tasks.

`conversation-guidance.md` must contain a fenced block tagged `first-message` — the text inside is sent verbatim as the assistant's first turn. The rest of the file is plain instructions to the chat model (persona, tone, conversation goal, pacing rules).

`grading-guidance.md` is the rubric: a section per category (matching `task.yml` `categories`), describing what each performance band looks like. The judge returns a band, a score 0–100, an evidence line, and a concerns line per dimension.

The repo ships two example tasks you can copy and adapt:

- [config/tasks/matmul/](config/tasks/matmul/) — a minimal example (matrix multiplication problem set) used by [TUTORIAL.md](TUTORIAL.md).
- [config/tasks/poster/](config/tasks/poster/) — a richer example with seven required conversation areas.

## Local development

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env`:

```bash
OPENAI_API_KEY=sk-...
ADMIN_PASSWORD=change-this-password
SESSION_SECRET=change-this-random-string
DATA_DIR=data
MAX_ATTEMPTS_PER_STUDENT=2
```

Run:

```bash
uvicorn app.main:app --reload
```

- Student view: `http://127.0.0.1:8000` (try the demo code `poster-001` after editing `data/poster/student_codes.csv`; on a fresh checkout that file is seeded from `config/tasks/poster/student_codes.csv` on first launch).
- Admin: `http://127.0.0.1:8000/admin` (basic auth: `admin` / `ADMIN_PASSWORD`).

On first boot the app copies each `config/tasks/<id>/` into `data/<id>/` (the local stand-in for the persistent volume). After that, edits via the admin UI write to `data/<id>/`; the in-repo `config/tasks/*` only matters as the seed for a fresh/empty task folder.

See [`app/settings.py`](app/settings.py) for the complete list of environment variables (model overrides, idle-session timeout, upload size limits, etc.).

## OpenAI configuration

```bash
OPENAI_API_KEY=...
OPENAI_CHAT_MODEL=gpt-5.4-mini       # runs the student conversation
OPENAI_JUDGE_MODEL=gpt-5.5           # produces the per-dimension grades
```

The judge receives the uploaded artifact (if any), the full transcript, the configured category names, and `grading-guidance.md`. It returns strict JSON validated against a schema generated from the task's categories ([app/models.py](app/models.py) `JudgeResult.json_schema_for_openai`).

## Hosting on Railway

Railway can deploy this repo directly from GitHub using the included [Procfile](Procfile):

```text
web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

Setup:

1. **Attach a persistent volume** to the service and mount it at `/app/data/`. Without a volume, every redeploy loses all grading data and admin edits.
2. **Set environment variables** in the Railway dashboard:
   - `OPENAI_API_KEY` — required.
   - `ADMIN_PASSWORD`, `SESSION_SECRET` — required.
   - `OPENAI_CHAT_MODEL`, `OPENAI_JUDGE_MODEL` — optional model overrides.
   - `MAX_ATTEMPTS_PER_STUDENT`, `DEFAULT_MIN_CHAT_SECONDS`, `DEFAULT_MIN_STUDENT_REPLIES`, `MAX_UPLOAD_MB`, `SESSION_IDLE_TIMEOUT_SECONDS` — optional. See [`app/settings.py`](app/settings.py).
3. **Deploy.** First boot seeds each task folder on the volume from the in-repo `config/tasks/<id>/`. Subsequent boots only seed task folders that don't yet have a `task.yml`.

All admin edits, instructor grades, conversations, and uploads are written to the volume.

## GitHub sync

[`.github/workflows/sync-railway-volume.yml`](.github/workflows/sync-railway-volume.yml) backs the volume up to git. It runs nightly at 03:00 UTC and can be triggered manually from the Actions tab.

The job SSHes into the Railway service, tars the volume, and extracts it back into the repo:

- Everything goes into `data/<task_id>/...` (one folder per task containing task config files, `ai_marks.csv`, `conversations/`, and `uploads/`).
- The four per-task config files (`task.yml`, `student_codes.csv`, `conversation-guidance.md`, `grading-guidance.md`) are also copied into `config/tasks/<task_id>/` so the seed reflects the latest production state.

If anything changed, the workflow commits with the message `Sync Railway volume files` and pushes to `main`.

Required GitHub secrets:

- `RAILWAY_API_TOKEN` — for the Railway CLI.
- `RAILWAY_SSH_PRIVATE_KEY` — SSH key registered with the Railway service.

Required workflow env vars (already set in the file): `PROJECT_ID`, `ENVIRONMENT_ID`, `SERVICE_ID`. Update these if you fork the repo.

The repo therefore has a git-backed audit trail of every admin edit and every grading session, with the seed always matching what was running in production at the last sync.

## Outputs

```text
data/<task_id>/ai_marks.csv               # one row per (session, dimension)
data/<task_id>/conversations/*.json       # full transcripts + judge result
data/<task_id>/uploads/*                  # student-submitted files
```

`ai_marks.csv` columns: `timestamp, task_id, task_title, session_id, attempt_number, student_code, student_name, student_id, dimension_name, ai_score, ai_band, ai_evidence, ai_concerns, instructor_score, review_status, transcript_turns, transcript_json, conversation_file, artifact_file, config_source`.

The CSV is advisory. Review it (and edit instructor grades inline from the admin page or by editing the CSV directly) before committing final marks.

## Forking this repo for your own deployment

This repository is public for reference and reuse — it does **not** itself run a deployment. If you want to use this tool with real students, fork it to a **private** repository and deploy from there. Real student data (grades, names, conversations, uploads) must never live in a public repo, both for privacy reasons (FERPA, GDPR, etc.) and because the nightly sync workflow will otherwise commit that data straight to git history.

Recommended setup if you want to keep getting updates from this public repo while running your own private instance:

```bash
# 1. Create a new private repo on GitHub (e.g. yourname/ai-grader-instance).
# 2. Clone this public repo and re-target the default remote at your private one.
git clone https://github.com/benrosche/ai-grader-public.git ai-grader-instance
cd ai-grader-instance
git remote remove origin
git remote add origin https://github.com/<you>/ai-grader-instance.git
git push -u origin main

# 3. Keep a second remote pointing at this public repo so you can pull updates.
git remote add public https://github.com/benrosche/ai-grader-public.git
git remote -v   # verify: 'origin' = your private, 'public' = this repo
```

From then on:

- Run the app, deploy to Railway, and run the nightly sync workflow from **your private repo only**. Set `RAILWAY_API_TOKEN` and `RAILWAY_SSH_PRIVATE_KEY` as secrets there.
- When this public repo gets a new feature or fix, bring it into your private repo with:
  ```bash
  git pull public main
  ```
  Git will merge the public-side code changes into your private branch. Code lives in `app/` and templates; your student data and task config live under `data/` (and the per-task seed under `config/tasks/`), so merges are conflict-free in normal use.
- Develop changes you'd like to share back to the public repo by working in a separate clone of the public repo, not in your private one.

## License

Released under the [Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)](https://creativecommons.org/licenses/by-nc/4.0/) license. You may copy, modify, and redistribute this code with attribution, but not for commercial purposes.
