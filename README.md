# TTRPG recording & transcription pipeline (v3)

```
 LAPTOP (Windows, PowerShell)                    DOCKER HOST (behind Nginx Proxy Manager)
 ─────────────────────────────                   ────────────────────────────────────────
 record.ps1      crash-safe FLAC segments         FastAPI + job queue (one job at a time)
      │                                            sync (raw uploads only) → preprocess → mix
 Sync-TTRPGLavs.ps1   clap sync → 16 kHz FLAC       → WhisperX → speaker assignment
      │                                            → Nextcloud (WebDAV) → keep only 4 files
 Submit-Session.ps1 ── resumable upload ──────►  Dashboard: jobs, progress, logs, retry,
      (API key)          + progress follow         manual upload, API keys, users
```

- 

**Laptop:** records and syncs, then uploads about 170 MB of FLAC per hour (was about 445 MB of WAV). Every source file stays on the laptop, so any session can be submitted again.

- **Server:** does all the heavy lifting. After a successful job it keeps only `lav_mix.flac` and `transcript_lav_assigned.{csv,md,json}`. A failed job keeps everything so it can be retried.
- **Results:** go to Nextcloud under `Documents/Riftward/<session date>/`. The `.md` is also copied to `Riftward/Claude/Session Logs/<session date>_transcript_lav_assigned.md`. Folders are named by the **recording** date, not the processing date.

---

## 1. Server setup (Docker host)

1. Copy the `server/` folder to the Docker host, e.g. `/opt/ttrpg`.
2. `cp .env.example .env` and edit it. At minimum set:
   - `BASE_URL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`
   - `NEXTCLOUD_URL`, `NEXTCLOUD_USER`, `NEXTCLOUD_APP_PASSWORD` (Nextcloud → Settings → Security → *Create new app password*)
   - `FORWARDED_ALLOW_IPS` = the address or subnet NPM connects from
3. **CPU or GPU?** Run `nvidia-smi` on the host.
   - **No NVIDIA GPU:**

     ```
     docker compose up -d --build
     ```

     The default CPU model is `small`, which is what your old runs used. Expect it to take about as long as the laptop did.
   - **NVIDIA GPU** (needs the driver plus `nvidia-container-toolkit`):

     ```
     docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
     ```

     It uses `large-v3`, which should take a few minutes per hour of audio.
   - The server always picks CUDA automatically when it can see a GPU (`WHISPER_DEVICE=auto`).
4. The first job downloads the Whisper and alignment models into `./data/models`. That takes a while once; after that they're cached.
5. **Nginx Proxy Manager:** add a Proxy Host for `http://<docker-host>:8054` (the port is `HOST_PORT` in `.env`) with SSL.
   - Uploads are sent in 16 MB chunks. If NPM rejects them (HTTP 413), add `client_max_body_size 100m;` under *Advanced*.
   - If NPM runs on the same host, you can publish the port only locally (`127.0.0.1:${HOST_PORT:-8054}:8000` in `docker-compose.yml`), or put both containers on one Docker network. That way nobody can skip the proxy.
6. Open the site and sign in as the admin. Then go to **API keys → Create key** and copy the key to the laptop's `config.json`.

### Sign-in

- **Local accounts:** the admin from `.env` is created on first start. Add more users under **Users**.
- **Nextcloud sign-in (optional):**
  1. Install the *OIDC Identity Provider* app on Nextcloud.
  2. Create a client whose redirect URI is `https://<BASE_URL>/auth/oidc/callback`.
  3. Set the `OIDC_*` values in `.env`.
- **Who can use Nextcloud sign-in:**
  - Users listed in `OIDC_ALLOWED_USERS`, or members of `OIDC_ALLOWED_GROUPS`, get an account on first sign-in.
  - An account an admin added with **no password** links on its first Nextcloud sign-in.
  - Accounts that have a password, including the admin, must link from **Account → Link Nextcloud account**. This stops a Nextcloud user named "admin" from taking over the local admin.
- **API keys** are for machines only. They can create jobs, upload and read status. They can't sign in to the dashboard, retry or delete jobs, or manage users or keys.
- Failed logins are rate-limited per IP and username.
- **Permissions** (Users page) are three tick boxes per user. They also cover the API keys that user creates (laptop scripts).
  - **Admin:** manages users and API keys, and can open, cancel, retry and delete every job.
  - **See all outputs:** can open other people's transcripts, downloads and logs. Without it (or admin) a user still sees every job in the queue with its progress, but only their own jobs' output, and can only manage their own jobs. Unfinished browser recordings are only listed for their owner and admins.
  - **Publish to Nextcloud:** on, their results are uploaded to the campaign folders in Nextcloud; off, results stay on the server and they download them from each job page.
  - New users start with all three off, so friends can use the server without seeing your sessions or anything landing in your campaign. On upgrade, existing accounts keep publishing, admins see everything, and existing jobs belong to whoever submitted them (for API-key jobs, the key's creator). You can't remove your own admin, and there is always at least one admin.

### Optional Nextcloud notification

Set `NEXTCLOUD_NOTIFY_ENABLED=true` and give it an **admin** app password. Nextcloud's admin-notification API requires one. You'll then get a bell notification when a job finishes or fails. If the notification fails, the job still succeeds.

---

## 2. Laptop setup

1. **Back up** the current recorder scripts. This replaces `record.ps1` and `Sync-TTRPGLavs.ps1`.
2. In the dashboard, open **Laptop scripts**:
   - **Download with a new API key** for a new laptop. The zip includes a ready-to-use `config.json` with the server address and a fresh key.
   - **Download scripts** to update an existing laptop. That zip has no `config.json`, so your settings are kept.
3. Right-click the zip → **Properties** → tick **Unblock**, then extract it straight into the recorder folder, next to `ffmpeg.exe` and `ffprobe.exe` (not included). The scripts also find `ffmpeg.exe` one folder up, or on PATH.
4. Check `playerCount` and `roomMic` in `config.json`. Without the key download, copy `config.example.json` to `config.json` and set `server.apiKey`. All settings:

| setting                    | meaning                                                                                                                                                   |
|----------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------|
| `mode`                     | `lavs` (normal) or `single` (room mic only, see section 3a). `record.ps1 -Single` / `-Lavs` override it per session                                       |
| `playerCount`              | number of lavs, counting the DM's (Player1..N). Default 4 = 3 players + DM. Receivers needed = ceil(players ÷ `channelsPerReceiver`)                      |
| `channelsPerReceiver`      | 2 for your stereo receivers                                                                                                                               |
| `roomMic`                  | `optional` / `required` / `none`                                                                                                                          |
| `segmentSeconds`           | recording segment length (crash loses at most a fraction of the current one)                                                                              |
| `stallSeconds`             | a stream that stops growing this long is restarted                                                                                                        |
| `sync.windowSeconds`       | how long after the start of each part to look for the clap                                                                                                |
| `processing.whisperModel`  | blank = server default; e.g. `medium`, `large-v3`                                                                                                         |
| `processing.enrollment`    | optional, e.g. `["Player1=00:00:10-00:00:35"]` (usually not needed now)                                                                                   |
| `processing.diarize`       | room-mic-only sessions: have the server guess speakers (needs `HF_TOKEN` on the server)                                                                   |
| `processing.peopleAtTable` | room-mic-only + diarize: everyone who talks, **players plus the DM**. Default 4 (3 players + DM). 0 = asked at upload (blank there lets the server guess) |

Python is **no longer needed** on the laptop.

`Submit-Session.ps1` warns when the server has newer scripts than the laptop (it compares `client_version.txt` with the server).

---

## 3. Running a session

```powershell
.\record.ps1          # choose devices (first time), mic check, then CLAP once
                      # Q (or Ctrl+C) -> confirm -> "Sync and upload now? [Y/n]"
```

Pressing **Y** syncs the session and uploads it, then shows server progress until the transcript is in Nextcloud. Closing the window only stops the progress display; the job keeps running on the server. You can watch it on the dashboard at any time.

Other commands:

```powershell
.\Sync-TTRPGLavs.ps1 -SessionDir <folder> -Submit   # sync + upload a specific session (old sessions work too)
.\Sync-TTRPGLavs.ps1 -SessionDir <folder> -NoClap -Submit   # re-sync ignoring the clap (tracks as recorded)
.\Sync-TTRPGLavs.ps1 -SessionDir <folder> -ClapTimes Player1=4.04,Player2=4.05,Player3=4.03,Player4=4.06 -Submit
                                                     # re-sync with clap times read off in Audacity (2:Player1=... for part 2)
.\Submit-Session.ps1                                 # upload/resume the most recent synced session
.\Submit-Session.ps1 -SessionDir <folder> -Resubmit  # process a session again (new server job)
.\Submit-Session.ps1 -WhisperModel medium -Resubmit  # ...with different options
.\record.ps1 -Reselect                               # choose devices again
.\record.ps1 -Single                                 # room mic only (see 3a)
```

**Manual upload:** use **Upload** in the dashboard. Pick one file per player.

- Choose **Raw** for the original recordings; the server finds the clap and syncs them. The **Clap** setting decides what happens next: use the clap if it lines up and otherwise keep the tracks as recorded (default), stop and ask you when it doesn't line up, always stop and ask, ignore the clap, or use clap times you type in (one per track, e.g. read off in Audacity).
- A raw job that stopped to ask shows **review** and a *Clap sync* panel on its page: the clap time found on each track, and a choice to use it, ignore it, or enter the times yourself. The same panel lets you re-sync a failed or cancelled raw job. Only the sync and the steps after it run again; the upload is reused.
- Choose **Already synced** for `synced_*.flac` files.

---

### 3a. Room mic only (best effort)

For a session without the lavs, record just the room mic:

```powershell
.\record.ps1 -Single                    # or set "mode": "single" in config.json
.\Submit-Session.ps1 -Diarize -People 5 # e.g. a guest: 4 players + DM (default is 4: 3 players + DM)
```

- It uses the room mic saved in `last_devices.json`; if there isn't one, it asks and saves the choice in `last_single_device.json`, so switching modes never wipes the lav receivers. `-Single -Reselect` picks again.
- No clap is needed. Crash-safety, resume and restarts work as in lav mode.
- If `record.ps1` finds only one microphone (or too few for the lav receivers), it asks whether to record with a single mic instead, so a missing receiver doesn't stop the session.
- **What you lose:** no player names. Without diarization the transcript is timestamped lines with no speakers. With diarization lines are labelled *Speaker 1, Speaker 2, ...*: guesses that can split or merge people, and they aren't matched to players. Diarization is also slower, especially on CPU.
- **Diarization setup (server, once):** make a free Hugging Face account, accept the conditions on <https://huggingface.co/pyannote/speaker-diarization-community-1>, create a *Read* token and put it in `.env` as `HF_TOKEN`. The model downloads on the first diarized job.
- Results use the same file names and Nextcloud folders as lav sessions (`lav_mix.flac` is then the room recording).
- In the dashboard, choose **Upload → Room mic only**. It asks for the number of people at the table, including the DM, which is used for speaker guessing.

### 3b. Recording in the browser (Record page)

The dashboard's **Record** page records straight from the browser: the lav receivers (a normal lav session) or one room mic (room-mic-only). It's handy when the laptop scripts aren't available.

- It needs the HTTPS address (browsers only allow microphones on secure pages) and a current Chrome or Edge. Close Teams, Discord or anything else that uses a receiver first; they block it.
- **Allow microphones** (one permission covers all of them), then choose what you're recording:
  - **Lav receivers:** pick the device for each receiver (default two; **Add receiver** for more). Each channel shows a level meter and an optional **player name**; blank uses Player1, Player2, ... in order, the same as the laptop scripts. Names become the speaker labels in the transcript (letters, digits, `_ . -`, no spaces). Tick **not used** for an empty channel. The page remembers the receivers and names; if a saved receiver isn't connected it asks you to choose the receivers again.
  - **Room mic only:** pick one microphone. Speaker guessing and the number of people at the table (including the DM) work as on the Upload page.
- **Start recording**, then **clap once, loudly** (lav mode). The server lines the receivers up on the clap at the start of each part when the claps agree within 1 s, otherwise on the browser's start times (usually within a few tens of ms). The **Clap** setting can switch the clap off.
- Audio is saved in the browser every 5 seconds and uploaded in the background. If the connection drops, recording carries on and the upload catches up later.
- If a receiver disappears or stops sending audio, it beeps and restarts all mics as a new part (clap again), retrying every 5 s until the mic is back.
- If the tab, browser or computer closes, open the Record page again: it offers **Resume recording (new part)**, **Finish & process** or **Discard**. Nothing already recorded is lost. Gaps between parts become silence, like the laptop sync.
- **Stop** uploads whatever is left, builds one synced track per player on the server and opens the new job. The job log lists the receivers, players and how each part was lined up.
- Recordings that were started but never finished (for example on another computer) are listed at the bottom of the page. Abandoned recordings are deleted after 14 days without new audio.
- Only one Record tab can be open at a time. Keep it open while recording (closing it stops the recording, unlike the laptop script); the screen is kept awake where the browser allows it.
- `tools\mic-test.html` (run `tools\Serve-MicTest.ps1`) checks that a browser gets both channels of each receiver.
- **Channels merged?** If each receiver's left and right meters move together, something in the browser is intercepting the microphone, usually an extension (a voice changer such as Clownfish, a noise canceller, a meeting recorder). The Record page shows a red warning when it detects this. Remove the extension or block its site access for this site, then reload.

### Forgot your password?

There's no reset link (the server doesn't send email). On the Docker host, in the server folder:

```bash
sh reset-password.sh            # lists the accounts and asks which one
sh reset-password.sh admin      # or name it
```

It asks for the new password twice (at least 10 characters) and takes effect immediately. It runs as `PUID:PGID` from `.env` so the database keeps the right owner. Without the script: `docker compose exec -u 1000:1000 ttrpg python -m app.manage reset-password admin` (and `... list-users`). Changing `ADMIN_PASSWORD` in `.env` doesn't reset anything; it's only used to create the first account.

### Updating the server (GitHub)

Pushing to `main` runs `.github/workflows/deploy.yml`: it installs the web app's dependencies, runs the tests in `server/tests`, waits until the server is idle, then triggers the deploy over SSH.

- **Waiting for idle:** `GET /api/v1/health/idle` returns 200 when nothing would be interrupted and 503 (with the reasons) while a job runs or is queued, a laptop upload has moved in the last 5 minutes, or a browser recording sent audio in the last 3 minutes. Set the repository variable `IDLE_URL` to `https://<your site>/api/v1/health/idle`; without it the workflow deploys straight away. It waits up to 6 hours (GitHub's limit). If several pushes pile up, only the newest one deploys.
- **If a restart happens anyway:** the server finishes in-flight requests (up to 20 s), stops the running job and puts it back in the queue. It resumes from the last finished stage, but a transcription that was cut off starts that stage over. Browser recordings keep recording into the browser and upload again once the server is back; a recording that was being built is released on startup and the browser retries it. The laptop scripts retry for about 7 minutes.
- **On the Docker host** the deploy needs to run, in the repo's `server` folder: `git pull` then `docker compose up -d --build`. `server/.env` and `server/data/` are not in git, so they survive every pull.

## 4. What happens when things go wrong

| situation                                               | what the recorder does                                                                                                                                                                                                                                                                                            |
|---------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Recorder window closed / PowerShell crashes             | ffmpeg keeps recording in the background (its own hidden console). Run `record.ps1` again: it re-attaches and you can stop it normally.                                                                                                                                                                           |
| A second `record.ps1` window is opened                  | It refuses to attach while the first window is still watching.                                                                                                                                                                                                                                                    |
| Laptop crash / power loss                               | Everything written so far is kept (FLAC segments are flushed packet by packet). Run `record.ps1`: **R** resumes the same session as a new part (clap again), **F** finishes it, **N** starts fresh.                                                                                                               |
| Receiver unplugged / ffmpeg dies / a stream stalls      | Beeps, restarts all streams as a new part, and asks for a clap. If the device is gone it keeps retrying every 10 s.                                                                                                                                                                                               |
| Clap missing or drowned out in a part                   | Sync shows the clap it found on each track and asks: **C** use it, **N** ignore it and keep the tracks as recorded (they start together, so they're usually within 0.1 s), **M** type each track's clap time yourself. If the claps don't line up, **N** is the default. `-Yes` takes the default without asking. |
| Saved devices don't match the config, or one is missing | `last_devices.json` is deleted and you choose every device again.                                                                                                                                                                                                                                                 |
| Upload interrupted                                      | Run the same command again; it resumes where it stopped.                                                                                                                                                                                                                                                          |
| Synced files changed after submitting                   | `Submit-Session.ps1` notices and creates a new job automatically.                                                                                                                                                                                                                                                 |
| Raw dashboard upload: clap didn't line up               | By default the server keeps the tracks as recorded and carries on. With **Clap: stop and ask me** the job waits (status *review*) until you choose on the job page.                                                                                                                                               |
| Server job fails                                        | The dashboard shows the error and log. **Retry** reuses finished stages; for example, it won't transcribe again if only the Nextcloud upload failed.                                                                                                                                                              |
| Server restarted mid-job                                | The job goes back to the queue automatically.                                                                                                                                                                                                                                                                     |

Parts are joined using their recorded start times, so the gap during a restart becomes silence. The timeline stays continuous and all players stay aligned.

---

## 5. Speaker assignment (what changed)

The old method labelled about 70% of lines as two speakers ("Player4+Player3"), and the step took about 9 minutes per session.

The new method:

- Measures each mic over every word's exact time span.
- Works out from the recording itself how much each player bleeds into the other mics.
- Picks the player who explains the levels on all mics.
- Smooths the result so single words don't jump between players.
- Splits Whisper lines that contain more than one speaker.

On your 10-15 session, roughly 90% of lines now get a single speaker. Lines still get "A+B" when two people are clearly talking at once. The step takes about 10 seconds.

Server-side tuning, per job (via the Submit script, or the API `options`): `overlap_db` (default 10), `label_overlaps`, `switch_cost_db` (12), `switch_cost_boundary_db` (3), `silence_db` (-60).

---

## 6. Files

```
server/          Dockerfile, docker-compose.yml, docker-compose.gpu.yml, .env.example, entrypoint.sh, app/
ROADMAP.md       planned work (Linux recorder, ...)
server/client/   laptop scripts (served by the dashboard): record.ps1, Sync-TTRPGLavs.ps1,
                 Submit-Session.ps1, TTRPG.Common.ps1, config.example.json
```

Session folder layout from the new recorder:

```
session_YYYY-MM-DD_HH-MM-SS\
  session_manifest.json        devices, players, parts (start times, why each ended)
  raw\part01\Player1_00000.flac ... room_00000.flac, ffmpeg_rx1.log ...
  raw\part02\...               only if the recording was restarted/resumed
  00_synced\synced_Player1.flac ... sync_manifest.json
  upload_state.json            which server job this session went to
```

Old sessions (`players\*.wav`) still sync and submit as before.