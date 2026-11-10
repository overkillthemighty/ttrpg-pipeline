# TTRPG Pipeline: notes for Claude Code

Records tabletop sessions (one lav mic per player, or a single room mic), transcribes them with WhisperX on a
Docker server, labels each line with the speaker, and publishes the results to Nextcloud. Owner: Jeff
(GitHub `overkillthemighty`). Repo is public (GPL-3.0); never commit secrets, `.env`, recordings or personal data.

## Layout

- `server/` Docker image (FastAPI + Jinja2 + SQLite, one uvicorn worker; the job queue lives in it)
  - `app/main.py` startup/shutdown, `web.py` pages, `api.py` REST API (`/api/v1/...`), `security.py` users,
    API keys, permissions, CSRF, `db.py` schema + migrations (ALTER TABLE in `init()`), `jobs.py` job model,
    `worker.py` runs one job at a time in a child process, `recordings.py` browser recordings (chunk upload,
    assembly, clap alignment), `manage.py` CLI (reset-password, list-users),
    `updates.py` update tracker (running commit from the `../.git` mount vs GitHub; admin banner, footer version)
  - `app/pipeline/` `runner.py` stages: validate, sync, preprocess, mix, transcribe (WhisperX CLI), assign
    (lav bleed matrix) or `single.py` (room mic, optional diarization), encode `lav_mix.flac`, publish (WebDAV)
  - `app/static/` `app.js` (dashboard), `record.js` (browser recorder, IndexedDB), `app.css`, `mic-test.html`
  - `client/` PowerShell laptop scripts (record.ps1, Sync-TTRPGLavs.ps1, Submit-Session.ps1, TTRPG.Common.ps1),
    served as a zip from /client (optionally with a prefilled config.json and new API key)
  - `tests/` pytest smoke tests; run without WhisperX or Nextcloud
- `tools/` mic test page + `Serve-MicTest.ps1` (localhost server for it)
- `.github/workflows/deploy.yml` CI + deploy

## Run the tests

```
cd server
pip install -r requirements.txt pytest
python -m pytest -q tests
```

## Deploy (push to main = production)

1. GitHub Actions: install, compile, pytest.
2. Waits until `vars.IDLE_URL` (`https://<site>/api/v1/health/idle`) returns `{"idle":true}`: no running/queued
   job, no laptop upload in the last 5 min, no browser recording chunk in the last 3 min. Up to 6 h.
   Fails fast if IDLE_URL is malformed.
3. `ssh -p 8022 deploy@ssh.jeffmunk.info ttrpg-pipeline` (secret `SSH_KEY`, forced command
   `/usr/local/bin/deploy-riftward-test.sh`, shared with the riftward-reloaded repo). The script does
   `git pull` in `/srv/ttrpg-pipeline` and `docker compose up -d --build` in `server/` (it falls back to
   `server/` when the repo root has no compose file).
4. On the host ("web"): container `ttrpg-transcriber`, port `HOST_PORT` (8054) behind Nginx Proxy Manager.
   `server/.env` and `server/data/` live only there (gitignored).

A restart during a job: uvicorn gets 20 s for in-flight requests, the running job goes back to the queue and
resumes from its last cached stage (an interrupted transcription restarts that stage). Browser recordings keep
recording locally and re-upload; laptop scripts retry ~7 min.

## Rules of thumb

- Recorder stability comes first. Never change the browser recording API in a way that breaks a page that was
  opened before a deploy (old record.js keeps talking to the new server).
- Static files are versioned (`web.asset()` adds `?v=hash`); always reference assets through it.
- All state-changing dashboard requests need the CSRF token; API keys are for machines only.
- Permissions: `is_admin`, `can_view_all` (see other users' outputs), `can_publish` (results to Nextcloud).
  Jobs and browser recordings have an `owner`; API-key jobs belong to the key's creator.
- Audio is FLAC 16-bit throughout (mixing s16/s32 broke concat once).
- Default table: 3 players + DM (`peopleAtTable` 4). Player names default to Player1..N.
- Keep `.sh`, `.py`, Dockerfile, `.yml` with LF endings (`.gitattributes`); `.ps1` CRLF.
- Changing anything in `server/client/` changes the client version; laptops get a "newer scripts" warning.
- Write user-facing text plainly; README is the user manual, keep it in step with behaviour.

## Open ideas (not started)

See ROADMAP.md: Linux client; parallel preprocessing; speaker summary table on the job page; in-progress
browser recordings in the Jobs list; room mic alongside lavs; transcribing in chunks so a restart loses less.
