/* Dashboard behaviour: live job polling, job actions, and the resumable browser upload. */
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const ACTIVE = new Set(["uploading", "queued", "running"]);

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000), p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function detailText(d) {
    if (!d) return "";
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => (x && x.msg ? `${(x.loc || []).slice(1).join(".")} ${x.msg}`.trim() : JSON.stringify(x))).join("; ");
    return JSON.stringify(d);
  }

  async function api(method, url, body, extraHeaders) {
    const headers = Object.assign({ "X-CSRF-Token": csrf }, extraHeaders || {});
    let payload = body;
    if (body && !(body instanceof Blob) && typeof body === "object") {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    const res = await fetch(url, { method, headers, body: payload, credentials: "same-origin" });
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON */ }
    if (res.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); }
    if (!res.ok) {
      const err = new Error(detailText(data && data.detail) || `HTTP ${res.status}`);
      err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------ job list
  function pollJobList() {
    const tbody = document.getElementById("job-rows");
    async function tick() {
      try {
        const jobs = await api("GET", "/api/v1/jobs?limit=200");
        if (!jobs.length) return;
        tbody.innerHTML = jobs.map((j) => `
          <tr data-id="${esc(j.id)}">
            <td><a href="/jobs/${esc(j.id)}">${esc(j.title)}</a><div class="muted small">${esc(j.id)}</div></td>
            <td>${esc(j.session_date)}</td>
            <td><span class="badge ${esc(j.status)}">${esc(j.status)}</span></td>
            <td><div class="bar ${esc(j.status)}"><span style="width:${Math.round(j.percent)}%"></span></div><div class="muted small">${esc(j.message || "")}</div></td>
            <td class="small">${fmtTime(j.created_at)}<div class="muted">${esc(j.submitted_by || "")}</div></td>
          </tr>`).join("");
      } catch (e) { /* keep last view; try again next tick */ }
    }
    setInterval(tick, 3000);
  }

  // ------------------------------------------------------------ job detail
  function pollJob(id, loadedStatus) {
    const $ = (s) => document.getElementById(s);
    const actions = document.getElementById("actions");
    const clapCard = $("clap-card");

    function setButtons(status) {
      const can = {
        cancel: ACTIVE.has(status) || status === "review",
        retry: status === "failed" || status === "cancelled",
        "retry-clean": status === "failed" || status === "cancelled",
        delete: status !== "running",
      };
      if (!actions) return;
      actions.querySelectorAll("button").forEach((b) => { b.disabled = !can[b.dataset.act]; });
    }

    async function tick() {
      let j;
      try { j = await api("GET", `/api/v1/jobs/${id}`); } catch (e) { return; }
      $("status").className = "badge " + j.status;
      $("status").textContent = j.status;
      $("stage").textContent = j.stage || "";
      $("pct").textContent = (j.percent || 0).toFixed(1) + "%";
      $("bar").style.width = (j.percent || 0) + "%";
      $("barwrap").className = "bar big " + j.status;
      $("message").textContent = j.message || "";
      $("error").hidden = !j.error;
      $("error").textContent = j.error || "";
      const log = $("log");
      if (log && j.can_see_output) {
        const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
        log.textContent = j.log_tail || "";
        if (atBottom) log.scrollTop = log.scrollHeight;
      }
      if (j.can_see_output) document.querySelectorAll("#results li").forEach((li) => {
        const n = li.dataset.name;
        li.innerHTML = j.final_files.includes(n) ? `<a href="/api/v1/jobs/${esc(id)}/final/${esc(n)}">${esc(n)}</a>` : `<span class="muted">${esc(n)}</span>`;
      });
      if (j.can_see_output && j.results && j.results.nextcloud && $("published")) {
        $("published").innerHTML = `Published to Nextcloud: <code>${esc(j.results.nextcloud.folder)}</code>`;
      }
      setButtons(j.status);
      // The clap card is drawn by the server: refresh the page when the job stops or pauses.
      if (clapCard && loadedStatus && j.status !== loadedStatus && !ACTIVE.has(j.status)) { location.reload(); return; }
      if (ACTIVE.has(j.status)) setTimeout(tick, 2000); else setTimeout(tick, 15000);
    }

    const clapForm = $("clap-form");
    if (clapForm) {
      const times = $("clap-times");
      const choice = () => (clapForm.querySelector('input[name="clap"]:checked') || {}).value;
      clapForm.addEventListener("change", () => { times.hidden = choice() !== "manual"; });
      clapForm.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const clap = choice();
        if (!clap) { alert("Choose how to sync."); return; }
        const body = { clap };
        if (clap === "manual") {
          body.clap_times = {};
          for (const inp of times.querySelectorAll("input[data-name]")) {
            const v = inp.value.trim();
            if (v === "" || isNaN(Number(v)) || Number(v) < 0) { alert(`Enter a clap time in seconds for ${inp.dataset.name}.`); inp.focus(); return; }
            body.clap_times[inp.dataset.name] = Number(v);
          }
        }
        const btn = clapForm.querySelector("button");
        btn.disabled = true;
        try { await api("POST", `/api/v1/jobs/${id}/sync`, body); location.reload(); }
        catch (e) { alert(e.message); btn.disabled = false; }
      });
    }

    if (actions) actions.addEventListener("click", async (ev) => {
      const b = ev.target.closest("button");
      if (!b) return;
      const act = b.dataset.act;
      if (act === "delete" && !confirm("Delete this job and all of its files on the server? (Nextcloud copies are not touched.)")) return;
      if (act === "cancel" && !confirm("Cancel this job?")) return;
      b.disabled = true;
      try {
        if (act === "cancel") await api("POST", `/api/v1/jobs/${id}/cancel`);
        if (act === "retry") await api("POST", `/api/v1/jobs/${id}/retry`, { clean: false });
        if (act === "retry-clean") await api("POST", `/api/v1/jobs/${id}/retry`, { clean: true });
        if (act === "delete") { await api("DELETE", `/api/v1/jobs/${id}`); location.href = "/"; return; }
        tick();
      } catch (e) { alert(e.message); b.disabled = false; }
    });
    setButtons(document.getElementById("status").textContent.trim());
    tick();
  }

  // ------------------------------------------------------------ upload
  function defaultLabel(fileName, index) {
    const base = fileName.replace(/\.[^.]+$/, "");
    let m = base.match(/player[\s_-]*(\d+)/i);
    if (m) return "Player" + parseInt(m[1], 10);
    m = base.match(/^synced_(.+)$/i);
    if (m) return m[1].replace(/[^A-Za-z0-9_.-]/g, "_");
    return "Player" + (index + 1);
  }

  function initUpload(chunkMb) {
    const form = document.getElementById("upload-form");
    const input = document.getElementById("file-input");
    const table = document.getElementById("file-table");
    const tbody = table.querySelector("tbody");
    const status = document.getElementById("upload-status");
    const bar = document.getElementById("upload-bar");
    const btn = document.getElementById("upload-btn");
    const CHUNK = chunkMb * 1024 * 1024;
    let files = [];
    const isSingle = () => (new FormData(form).get("synced") || "") === "single";
    const isRaw = () => (new FormData(form).get("synced") || "") === "0";
    const clapSelect = form.querySelector('select[name="sync_clap"]');
    const manualClap = () => isRaw() && clapSelect.value === "manual";

    function applyMode() {
      const single = isSingle();
      document.getElementById("single-opts").hidden = !single;
      document.getElementById("clap-opts").hidden = !isRaw();
      document.getElementById("clap-manual-hint").hidden = !manualClap();
      document.getElementById("lav-hint").hidden = single;
      document.getElementById("file-label").textContent = single ? "One file (the room mic)" : "One file per player";
      input.multiple = !single;
      if (single && files.length > 1) files = files.slice(0, 1);
      if (single && files.length === 1 && /^Player\d+$/.test(files[0].label)) files[0].label = "Room";
      render();
    }
    form.querySelectorAll('input[name="synced"]').forEach((r) => r.addEventListener("change", applyMode));
    clapSelect.addEventListener("change", applyMode);

    function render() {
      const manual = manualClap();
      table.hidden = files.length === 0;
      table.querySelector("th.clap-col").hidden = !manual;
      tbody.innerHTML = files.map((f, i) => `
        <tr><td><input data-i="${i}" class="label" value="${esc(f.label)}" pattern="[A-Za-z0-9][A-Za-z0-9_.\\-]{0,63}" required></td>
        <td class="small">${esc(f.file.name)}</td><td class="small mono">${(f.file.size / 1048576).toFixed(1)} MB</td>
        <td${manual ? "" : " hidden"}><input data-c="${i}" class="clap" type="number" step="0.001" min="0" value="${esc(f.clap || "")}" style="width:7em"${manual ? " required" : ""}></td>
        <td><button type="button" class="small" data-rm="${i}">Remove</button></td></tr>`).join("");
    }
    input.addEventListener("change", () => {
      const picked = Array.from(input.files).sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
      if (isSingle()) files = picked.slice(0, 1).map((file) => ({ file, label: "Room" }));
      else files = files.concat(picked.map((file, i) => ({ file, label: defaultLabel(file.name, files.length + i) })));
      input.value = "";
      render();
    });
    tbody.addEventListener("input", (e) => {
      if (e.target.classList.contains("label")) files[+e.target.dataset.i].label = e.target.value.trim();
      if (e.target.classList.contains("clap")) files[+e.target.dataset.c].clap = e.target.value.trim();
    });
    tbody.addEventListener("click", (e) => { const i = e.target.dataset.rm; if (i !== undefined) { files.splice(+i, 1); render(); } });

    async function sendFile(jobId, f, doneBefore, total) {
      const url = `/api/v1/jobs/${jobId}/files/${encodeURIComponent(f.label)}`;
      let offset = (await api("GET", url)).received;
      let failures = 0;
      while (offset < f.file.size) {
        const end = Math.min(offset + CHUNK, f.file.size);
        try {
          const r = await api("PATCH", url, f.file.slice(offset, end), { "Upload-Offset": String(offset), "Content-Type": "application/octet-stream" });
          offset = r.received; failures = 0;
        } catch (e) {
          if (e.status === 409 && e.data && typeof e.data.received === "number") { offset = e.data.received; continue; }
          if (e.status && e.status < 500 && e.status !== 408 && e.status !== 429) throw e;
          failures++;
          const wait = Math.min(30, 2 ** failures);
          status.textContent = `Connection problem on ${f.label}; retrying in ${wait}s (attempt ${failures})...`;
          await new Promise((r) => setTimeout(r, wait * 1000));
          try { offset = (await api("GET", url)).received; } catch (_) { /* retry loop */ }
          continue;
        }
        const pct = ((doneBefore + offset) / total) * 100;
        bar.style.width = pct.toFixed(1) + "%";
        status.textContent = `Uploading ${f.label}: ${(offset / 1048576).toFixed(0)} / ${(f.file.size / 1048576).toFixed(0)} MB`;
      }
    }

    window.addEventListener("beforeunload", (e) => { if (btn.disabled) { e.preventDefault(); e.returnValue = ""; } });

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!files.length) { alert("Pick at least one audio file."); return; }
      const single = isSingle();
      if (single && files.length !== 1) { alert("Room-mic-only mode takes exactly one file."); return; }
      const labels = files.map((f) => f.label.toLowerCase());
      if (new Set(labels).size !== labels.length) { alert("Speaker labels must be unique."); return; }
      const fd = new FormData(form);
      const options = {};
      for (const k of ["whisper_model", "language", "sync_window_seconds", "sync_search_start_seconds", "enrollment"]) {
        const v = (fd.get(k) || "").toString().trim();
        if (v) options[k] = v;
      }
      options.publish = fd.get("publish") === "1";
      if (isRaw()) {
        options.sync_clap = clapSelect.value;
        if (clapSelect.value === "manual") {
          options.clap_times = {};
          for (const f of files) {
            const v = (f.clap || "").trim();
            if (v === "" || isNaN(Number(v)) || Number(v) < 0) { alert(`Enter a clap time in seconds for ${f.label}.`); return; }
            options.clap_times[f.label] = Number(v);
          }
        }
      }
      if (single) {
        options.mode = "single";
        if (fd.get("diarize") === "1") {
          options.diarize = true;
          const people = (fd.get("people") || "").toString().trim();  // players + DM
          if (people) { options.min_speakers = people; options.max_speakers = people; }
        }
      }
      const body = {
        title: (fd.get("title") || "").toString().trim() || null,
        session_date: fd.get("session_date"),
        synced: single || fd.get("synced") === "1",
        client_ref: "dashboard upload",
        files: files.map((f) => ({ name: f.label, filename: f.file.name, size: f.file.size })),
        options,
      };
      btn.disabled = true;
      try {
        status.textContent = "Creating job...";
        const job = await api("POST", "/api/v1/jobs", body);
        const total = files.reduce((s, f) => s + f.file.size, 0);
        let done = 0;
        for (const f of files) { await sendFile(job.id, f, done, total); done += f.file.size; }
        status.textContent = "Queuing...";
        await api("POST", `/api/v1/jobs/${job.id}/submit`);
        btn.disabled = false;
        location.href = `/jobs/${job.id}`;
      } catch (e) {
        btn.disabled = false;
        status.textContent = "Upload failed: " + e.message;
      }
    });
  }

  // ------------------------------------------------------------ theme switch (Auto -> Light -> Dark)
  (function themeToggle() {
    const btn = document.getElementById("theme-toggle");
    if (!btn) return;
    const order = ["auto", "light", "dark"], label = { auto: "Auto", light: "Light", dark: "Dark" };
    const current = () => document.documentElement.dataset.theme || "auto";
    const show = () => { btn.textContent = "Theme: " + label[current()]; };
    btn.addEventListener("click", () => {
      const next = order[(order.indexOf(current()) + 1) % order.length];
      if (next === "auto") delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = next;
      try { if (next === "auto") localStorage.removeItem("theme"); else localStorage.setItem("theme", next); } catch (e) {}
      show();
    });
    show();
  })();

  window.TTRPG = { api, pollJobList, pollJob, initUpload };
})();
