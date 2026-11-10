/* Record page: lav receivers (one recording per receiver, a player per channel) or one room mic.

   Reliability rules:
   - Every chunk (5 s of one mic) is written to IndexedDB first and deleted only after the server
     confirms it, so a dropped connection, a server restart or a closed tab never loses audio.
   - The recording id is made here, so recording starts even if the server is unreachable.
   - All mics of a part start together. If any mic disappears or stops delivering audio, all of them
     restart as a new "part"; the server lines the parts up by their start times (and the clap).
   - Uploads never navigate away from the page (a sign-out mid-session only pauses uploading).
*/
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const TIMESLICE_MS = 5000;
  const STALL_MS = 20000;
  const RETRY_DEVICE_MS = 5000;
  const BITRATE = { 1: 96000, 2: 128000 };
  const CONFIG_KEY = "ttrpg-rec-config";
  let csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  // ------------------------------------------------------------ helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const pad = (n) => String(n).padStart(2, "0");
  const hms = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`; };
  const mb = (n) => (n / 1048576).toFixed(1) + " MB";
  const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
  function newId() {
    const a = new Uint8Array(6); crypto.getRandomValues(a);
    const d = new Date();
    return `rec-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}-` +
      Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  function setError(msg) { const e = $("rec-error"); e.hidden = !msg; e.textContent = msg || ""; }
  function setState(msg) { $("rec-state").textContent = msg; }
  function loadConfig() { try { return JSON.parse(localStorage.getItem(CONFIG_KEY)) || {}; } catch (_) { return {}; } }
  function saveConfig(c) { try { localStorage.setItem(CONFIG_KEY, JSON.stringify(c)); } catch (_) { /* private mode */ } }
  function beep() {
    try {
      const ctx = new AudioContext(), o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880; g.gain.value = 0.15; o.connect(g); g.connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 0.3); setTimeout(() => ctx.close(), 600);
    } catch (_) { /* no audio output */ }
  }
  let clapTimer = null;
  function clapPrompt() {
    const el = $("rec-clap-now"); el.hidden = false;
    clearTimeout(clapTimer); clapTimer = setTimeout(() => { el.hidden = true; }, 10000);
  }

  function detailText(d) {
    if (!d) return "";
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => (x && x.msg ? `${(x.loc || []).slice(1).join(".")} ${x.msg}`.trim() : JSON.stringify(x))).join("; ");
    return JSON.stringify(d);
  }

  // Fetch that never redirects (a redirect would end the recording) and refreshes the CSRF token once.
  async function call(method, url, body, headers, retried) {
    const h = Object.assign({ "X-CSRF-Token": csrf }, headers || {});
    let payload = body;
    if (body && !(body instanceof Blob) && typeof body === "object") { h["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    const res = await fetch(url, { method, headers: h, body: payload, credentials: "same-origin", redirect: "manual" });
    let data = null;
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    if (res.status === 403 && !retried && data && /CSRF/i.test(detailText(data.detail))) {
      try { const r = await fetch("/api/v1/csrf", { credentials: "same-origin", redirect: "manual" }); if (r.ok) csrf = (await r.json()).csrf; } catch (_) { /* offline */ }
      return call(method, url, body, headers, true);
    }
    if (!res.ok) {
      const err = new Error(detailText(data && data.detail) || (res.type === "opaqueredirect" ? "Signed out" : `HTTP ${res.status}`));
      err.status = res.status || (res.type === "opaqueredirect" ? 401 : 0); err.data = data;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------ IndexedDB
  let dbp = null;
  function idb() {
    if (dbp) return dbp;
    dbp = new Promise((resolve, reject) => {
      const r = indexedDB.open("ttrpg-recorder", 2);
      r.onupgradeneeded = () => {
        const d = r.result;
        for (const n of Array.from(d.objectStoreNames)) d.deleteObjectStore(n);   // v1 was a single-mic test layout
        d.createObjectStore("sessions", { keyPath: "rid" });
        const c = d.createObjectStore("chunks", { keyPath: ["rid", "part", "stream", "seq"] });
        c.createIndex("rid", "rid");
      };
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error);
    });
    return dbp;
  }
  async function tx(store, mode, fn) {
    const d = await idb();
    return new Promise((resolve, reject) => {
      const t = d.transaction(store, mode), s = t.objectStore(store);
      let out;
      Promise.resolve(fn(s)).then((v) => { out = v; });
      t.oncomplete = () => resolve(out);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error || new Error("IndexedDB transaction aborted"));
    });
  }
  const req = (r) => new Promise((resolve, reject) => { r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error); });
  const putSession = (s) => tx("sessions", "readwrite", (st) => req(st.put(s)));
  const getSessions = () => tx("sessions", "readonly", (st) => req(st.getAll()));
  const getSession = (rid) => tx("sessions", "readonly", (st) => req(st.get(rid)));
  const putChunk = (c) => tx("chunks", "readwrite", (st) => req(st.put(c)));
  const delChunk = (c) => tx("chunks", "readwrite", (st) => req(st.delete([c.rid, c.part, c.stream, c.seq])));
  const firstChunk = () => tx("chunks", "readonly", (st) => new Promise((resolve, reject) => {
    const r = st.openCursor(); r.onsuccess = () => resolve(r.result ? r.result.value : null); r.onerror = () => reject(r.error);
  }));
  // Chunks that couldn't be saved in the browser (storage full or broken): kept in memory and sent straight to the server.
  const memQueue = [];
  async function pendingInfo() {
    let out = { count: 0, bytes: 0, byRid: {} };
    try { out = await idbPending(); } catch (_) { /* storage unavailable */ }
    for (const c of memQueue) { out.count++; out.bytes += c.blob.size; out.byRid[c.rid] = (out.byRid[c.rid] || 0) + 1; }
    return out;
  }
  const idbPending = () => tx("chunks", "readonly", (st) => new Promise((resolve, reject) => {
    const out = { count: 0, bytes: 0, byRid: {} };
    const r = st.openCursor();
    r.onsuccess = () => {
      const c = r.result;
      if (!c) return resolve(out);
      out.count++; out.bytes += c.value.blob.size; out.byRid[c.value.rid] = (out.byRid[c.value.rid] || 0) + 1;
      c.continue();
    };
    r.onerror = () => reject(r.error);
  }));
  async function deleteLocal(rid) {
    await tx("chunks", "readwrite", (st) => new Promise((resolve, reject) => {
      const r = st.index("rid").openCursor(IDBKeyRange.only(rid));
      r.onsuccess = () => { const c = r.result; if (!c) return resolve(); c.delete(); c.continue(); };
      r.onerror = () => reject(r.error);
    }));
    await tx("sessions", "readwrite", (st) => req(st.delete(rid)));
  }

  // ------------------------------------------------------------ uploader
  const registered = new Set();
  let uploading = false, uploadedThisPage = 0, lastUploadError = "";
  function kick() { if (!uploading) uploadLoop(); }

  function serverBody(sess) {
    return {
      session_date: sess.session_date, title: sess.title, mode: sess.mode, clap: sess.clap, options: sess.options,
      layout: sess.layout.map((s) => ({ stream: s.stream, device: s.device_label, channels: s.channels, names: s.names })),
    };
  }
  async function register(sess) {
    await call("PUT", `/api/v1/recordings/${sess.rid}`, serverBody(sess));
    registered.add(sess.rid);
  }

  async function uploadLoop() {
    uploading = true;
    let failures = 0;
    try {
      for (;;) {
        let c = memQueue[0] || null;
        const fromMem = !!c;
        if (!c) { try { c = await firstChunk(); } catch (_) { c = null; } }
        if (!c) { lastUploadError = ""; break; }
        const drop = async () => { if (fromMem) memQueue.shift(); else await delChunk(c); };
        try {
          if (!registered.has(c.rid)) {
            const sess = (current && current.rid === c.rid) ? current : await getSession(c.rid).catch(() => null);
            if (!sess) { await drop(); continue; }   // orphan of a discarded recording
            await register(sess);
          }
          await call("PUT", `/api/v1/recordings/${c.rid}/parts/${c.part}/streams/${c.stream}/chunks/${c.seq}`, c.blob,
            { "Content-Type": "application/octet-stream", "X-Stream-Started-At": String(c.started_at_ms), "X-Stream-Mime": c.mime });
          await drop();
          uploadedThisPage++; failures = 0; lastUploadError = "";
        } catch (e) {
          if (e.status === 409 && /done|finishing/.test(e.message)) { await drop(); continue; }  // finished elsewhere
          if (e.status === 404) registered.delete(c.rid);
          failures++;
          lastUploadError = e.status === 401 ? "Signed out: sign in again in another tab. Recording continues; uploads resume after you sign in."
            : (!navigator.onLine ? "Offline: audio is kept in this browser and uploads when the connection is back."
              : `Upload problem (${e.message}); retrying.`);
          renderUpload();
          await sleep(Math.min(30000, 1000 * 2 ** Math.min(failures, 5)));
        }
        renderUpload();
      }
    } finally {
      uploading = false;
      renderUpload();
    }
  }
  window.addEventListener("online", kick);
  setInterval(kick, 15000);

  let lastPending = 0;   // chunks not yet on the server (for the leave-page warning)
  async function renderUpload() {
    try {
      const p = await pendingInfo();
      lastPending = p.count;
      const el = $("rec-upload");
      if (lastUploadError) { el.className = "small warn-text"; el.textContent = `${lastUploadError} Waiting: ${p.count} chunk(s), ${mb(p.bytes)}.`; return; }
      el.className = "small";
      el.textContent = p.count ? `Uploading: ${p.count} chunk(s) waiting (${mb(p.bytes)}).` :
        (uploadedThisPage ? "All recorded audio is on the server." : "Nothing to upload.");
    } catch (_) { /* ignore */ }
  }
  async function waitForUpload(rid) {
    for (;;) {
      const p = await pendingInfo();
      if (!p.byRid[rid]) return;
      kick();
      await sleep(1000);
    }
  }

  // ------------------------------------------------------------ devices, setup UI, meters
  let deviceList = [], permitted = false;
  const mode = () => document.querySelector('input[name="rec-mode"]:checked').value;
  const cfg = loadConfig();
  // receivers: [{ deviceId, names: [..], skip: [..], channels }]
  let receivers = Array.isArray(cfg.receivers) && cfg.receivers.length ? cfg.receivers : [{}, {}];

  function constraints(deviceId, stereo) {
    const clean = !stereo && $("rec-processing").checked;
    return { audio: { deviceId: { exact: deviceId }, channelCount: stereo ? { ideal: 2 } : 1,
      echoCancellation: clean, noiseSuppression: clean, autoGainControl: clean, voiceIsolation: clean } };
  }
  const label = (id) => (deviceList.find((d) => d.deviceId === id) || {}).label || "";

  async function listDevices() {
    const all = await navigator.mediaDevices.enumerateDevices();
    deviceList = all.filter((d) => d.kind === "audioinput" && d.deviceId && d.deviceId !== "default" && d.deviceId !== "communications");
    permitted = deviceList.length > 0 && deviceList.every((d) => d.label);
    $("rec-allow").hidden = permitted;
    $("rec-refresh").hidden = !permitted;
    $("rec-device-note").textContent = permitted ? `${deviceList.length} microphone(s) found.` :
      "Allow microphone access to list the microphones. One permission covers all of them.";
    // Saved receivers that aren't connected: clear them and choose again (like last_devices.json).
    const missing = receivers.filter((r) => r.deviceId && !deviceList.some((d) => d.deviceId === r.deviceId));
    const w = $("rec-saved-warning");
    if (permitted && missing.length) {
      w.hidden = false;
      w.textContent = `${missing.length} saved receiver(s) aren't connected (${missing.map((r) => r.label || "unknown").join(", ")}). Choose the receivers again.`;
      receivers.forEach((r) => { r.deviceId = ""; });
    } else w.hidden = true;
    if (permitted) {
      const used = new Set(receivers.map((r) => r.deviceId).filter(Boolean));
      for (const r of receivers) {
        if (r.deviceId) continue;
        const pick = deviceList.find((d) => !used.has(d.deviceId) && /rx|receiver|wireless|lav/i.test(d.label)) ||
          deviceList.find((d) => !used.has(d.deviceId));
        if (pick) { r.deviceId = pick.deviceId; used.add(pick.deviceId); }
      }
      const sel = $("rec-device");
      sel.innerHTML = deviceList.map((d) => `<option value="${esc(d.deviceId)}">${esc(d.label)}</option>`).join("");
      if (cfg.roomDevice && deviceList.some((d) => d.deviceId === cfg.roomDevice)) sel.value = cfg.roomDevice;
      sel.disabled = false;
    }
    renderReceivers();
    await preview();
  }

  // Player names: blank = Player<position> counting every channel in order.
  function defaultNames() {
    const out = []; let n = 0;
    receivers.forEach((r) => { const ch = r.channels || 2; const a = []; for (let i = 0; i < ch; i++) a.push(`Player${++n}`); out.push(a); });
    return out;
  }
  function effectiveNames(r, i) {
    const defs = defaultNames()[i];
    return defs.map((d, c) => ((r.skip || [])[c] ? null : (((r.names || [])[c] || "").trim() || d)));
  }

  function renderReceivers() {
    const box = $("rec-receivers");
    const defs = defaultNames();
    box.innerHTML = receivers.map((r, i) => {
      const ch = r.channels || 2;
      const opts = deviceList.map((d) => `<option value="${esc(d.deviceId)}"${d.deviceId === r.deviceId ? " selected" : ""}>${esc(d.label)}</option>`).join("");
      const rows = Array.from({ length: ch }, (_, c) => `
        <div class="rx-ch">
          <span class="muted small">${ch === 2 ? (c ? "Right / 2" : "Left / 1") : "Mono"}</span>
          <div class="meter"><span data-meter="rx${i + 1}:${c}"></span></div>
          <input data-rx="${i}" data-ch="${c}" class="rx-name" maxlength="64" placeholder="${esc(defs[i][c])}" value="${esc((r.names || [])[c] || "")}" aria-label="Player name">
          <label class="check small"><input type="checkbox" data-rx="${i}" data-skip="${c}"${(r.skip || [])[c] ? " checked" : ""}> not used</label>
        </div>`).join("");
      return `<div class="rx" data-i="${i}">
        <div class="row-between"><strong>Receiver ${i + 1}</strong>
          <div class="inline-form"><select data-rx-dev="${i}" ${permitted ? "" : "disabled"} aria-label="Receiver ${i + 1} device">${permitted ? opts : "<option>Allow microphones first</option>"}</select>
          ${receivers.length > 1 ? `<button type="button" class="small" data-rx-rm="${i}">Remove</button>` : ""}</div></div>
        ${rows}
        <div class="muted small" data-rx-note="${i}">${r.note || ""}</div>
      </div>`;
    }).join("");
    updateButtons();
  }

  function persist() {
    cfg.mode = mode();
    cfg.receivers = receivers.map((r) => ({ deviceId: r.deviceId, label: label(r.deviceId) || r.label, names: r.names || [], skip: r.skip || [], channels: r.channels }));
    cfg.roomDevice = $("rec-device").value || cfg.roomDevice;
    cfg.clap = $("rec-clap").value;
    cfg.processing = $("rec-processing").checked;
    saveConfig(cfg);
  }

  $("rec-receivers").addEventListener("input", (e) => {
    const t = e.target;
    if (t.classList.contains("rx-name")) {
      const r = receivers[+t.dataset.rx]; r.names = r.names || []; r.names[+t.dataset.ch] = t.value;
      t.classList.toggle("bad", !!t.value.trim() && !NAME_RE.test(t.value.trim()));
      persist(); updateButtons();
    }
  });
  $("rec-receivers").addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.skip !== undefined) { const r = receivers[+t.dataset.rx]; r.skip = r.skip || []; r.skip[+t.dataset.skip] = t.checked; persist(); updateButtons(); }
    if (t.dataset.rxDev !== undefined) { receivers[+t.dataset.rxDev].deviceId = t.value; receivers[+t.dataset.rxDev].channels = undefined; persist(); renderReceivers(); preview(); }
  });
  $("rec-receivers").addEventListener("click", (e) => {
    const i = e.target.dataset.rxRm;
    if (i !== undefined) { receivers.splice(+i, 1); persist(); renderReceivers(); preview(); }
  });
  $("rec-add-rx").onclick = () => {
    const used = new Set(receivers.map((r) => r.deviceId));
    const pick = deviceList.find((d) => !used.has(d.deviceId));
    receivers.push({ deviceId: pick ? pick.deviceId : "" });
    persist(); renderReceivers(); preview();
  };

  // Meters: one AudioContext; every open stream is split into its channels.
  let meterCtx = null, meterRaf = 0, meterNodes = [];
  function meterStreams(list) {   // list: [{ key: "rx1" | "room", stream }]
    stopMeters();
    try {
      meterCtx = new AudioContext();
      const pairs = [];   // stereo receivers: watch whether left and right are really different
      for (const { key, stream } of list) {
        const src = meterCtx.createMediaStreamSource(stream);
        const split = meterCtx.createChannelSplitter(2);
        src.connect(split);
        const nodes = [];
        for (let c = 0; c < 2; c++) {
          const an = meterCtx.createAnalyser(); an.fftSize = 1024;
          split.connect(an, c);
          const el = key === "room" ? (c === 0 ? $("rec-level") : null) : document.querySelector(`[data-meter="${key}:${c}"]`);
          const node = { an, el, buf: new Float32Array(an.fftSize) };
          nodes.push(node);
          if (el) meterNodes.push(node);
        }
        const note = key === "room" ? null : document.querySelector(`[data-rx-note="${+key.slice(2) - 1}"]`);
        if (note && document.querySelector(`[data-meter="${key}:1"]`)) pairs.push({ key, l: nodes[0], r: nodes[1], note, same: 0, active: 0, shown: false });
      }
      const draw = () => {
        for (const m of meterNodes) {
          m.an.getFloatTimeDomainData(m.buf);
          let p = 0; for (const v of m.buf) { const a = Math.abs(v); if (a > p) p = a; }
          const db = 20 * Math.log10(p || 1e-6);
          m.el.style.width = Math.max(0, Math.min(100, (db + 60) / 60 * 100)).toFixed(0) + "%";
          m.el.className = p > 0.97 ? "clip" : "";
        }
        for (const q of pairs) {
          const a = q.l.buf, b = q.r.buf;
          let energy = 0, diff = 0;
          for (let k = 0; k < a.length; k++) { energy += a[k] * a[k] + b[k] * b[k]; const d = a[k] - b[k]; diff += d * d; }
          if (energy / a.length > 1e-7) {
            q.active++;
            if (diff / (energy + 1e-12) < 1e-4) q.same++;
            if (q.active > 400) { q.active /= 2; q.same /= 2; }   // rolling
          }
          const merged = q.active >= 30 && q.same / q.active > 0.95;
          if (q.active >= 30 && diag[q.key]) diag[q.key].merged = merged;
          if (merged !== q.shown) {
            q.shown = merged;
            q.note.classList.toggle("err-text", merged);
            q.note.textContent = merged ? "Left and right are identical: the browser is merging this receiver's two channels, so its two players can't be told apart. See the tips under Refresh list." : "";
          }
        }
        meterRaf = requestAnimationFrame(draw);
      };
      draw();
    } catch (_) { /* meters are optional */ }
  }
  function stopMeters() {
    cancelAnimationFrame(meterRaf);
    if (meterCtx) { meterCtx.close().catch(() => {}); meterCtx = null; }
    meterNodes = [];
    document.querySelectorAll("[data-meter], #rec-level").forEach((el) => { el.style.width = "0%"; });
  }

  // Preview: open the chosen mics to show levels and find each receiver's channel count.
  let previewStreams = [], previewSeq = 0;
  // Extensions (voice changers, noise cancellers, meeting recorders) can replace getUserMedia and hand the page
  // their own processed audio: every device then comes back "stereo" with both channels identical.
  function micFunctionReplaced() {
    try { return !/\[native code\]/.test(Function.prototype.toString.call(navigator.mediaDevices.getUserMedia)); } catch (_) { return false; }
  }
  function showHooked(why) {
    const el = $("rec-hooked");
    el.hidden = false;
    el.textContent = `Something in this browser is intercepting the microphone (${why}). This is usually an extension such as a voice changer, ` +
      "noise canceller or meeting recorder. It gives this page processed audio, which merges each receiver's two players into one. " +
      "Remove it, or block it from this site (chrome://extensions > the extension > Details > Site access), then reload this page.";
  }
  const diag = {};   // what Chrome reports for each open receiver (for "Copy diagnostics")
  function diagText() {
    const lines = [`Record page diagnostics ${new Date().toISOString()}`, `Browser: ${navigator.userAgent}`, `Page: ${location.origin}`,
      `Microphone function: ${micFunctionReplaced() ? "REPLACED by something in the browser (extension?)" : "native"}`];
    for (const [k, d] of Object.entries(diag)) {
      const st = d.settings || {};
      lines.push(`${k}: ${d.device}`,
        `    settings: ${JSON.stringify(st)}`,
        `    asked for: ${JSON.stringify(d.constraints)}`,
        `    capabilities: ${JSON.stringify(d.capabilities && { channelCount: d.capabilities.channelCount, echoCancellation: d.capabilities.echoCancellation, voiceIsolation: d.capabilities.voiceIsolation })}`,
        `    left/right identical: ${d.merged == null ? "not measured yet" : d.merged ? "YES (merged)" : "no (true stereo)"}`);
    }
    return lines.join("\n");
  }
  function stopPreview() { previewStreams.forEach((s) => s.stream.getTracks().forEach((t) => t.stop())); previewStreams = []; stopMeters(); }
  async function preview() {
    if (live || restarting || !permitted) return;
    const my = ++previewSeq;
    stopPreview();
    const opened = [];
    try {
      if (mode() === "single") {
        const id = $("rec-device").value;
        if (id) opened.push({ key: "room", stream: await navigator.mediaDevices.getUserMedia(constraints(id, false)) });
      } else {
        let changed = false;
        for (let i = 0; i < receivers.length; i++) {
          const r = receivers[i];
          if (!r.deviceId) continue;
          try {
            const s = await navigator.mediaDevices.getUserMedia(constraints(r.deviceId, true));
            const tr = s.getAudioTracks()[0], st = tr.getSettings();
            // A real microphone always reports its clean-up settings; a stream made by other code doesn't.
            if (st.echoCancellation === undefined && st.autoGainControl === undefined) showHooked("the microphones come back without their normal settings");
            const ch = Math.max(1, Math.min(2, st.channelCount || 1));
            if (r.channels !== ch) { r.channels = ch; changed = true; }
            diag[`rx${i + 1}`] = { device: label(r.deviceId), settings: st, constraints: tr.getConstraints(),
              capabilities: tr.getCapabilities ? tr.getCapabilities() : null };
            r.note = ch === 1 ? "This device delivers one channel (mono): one player." : "";
            opened.push({ key: `rx${i + 1}`, stream: s });
          } catch (e) { r.note = `Can't open this device: ${e.message}`; changed = true; }
        }
        if (changed) { persist(); renderReceivers(); }
      }
    } catch (e) { setError("Can't open the microphone: " + e.message); }
    if (my !== previewSeq) { opened.forEach((o) => o.stream.getTracks().forEach((t) => t.stop())); return; }
    previewStreams = opened;
    meterStreams(opened);
    updateButtons();
  }

  function applyMode() {
    const m = mode();
    $("rec-lavs").hidden = m !== "lavs";
    $("rec-single").hidden = m !== "single";
    persist();
    preview();
    updateButtons();
  }
  document.querySelectorAll('input[name="rec-mode"]').forEach((r) => r.addEventListener("change", applyMode));

  // Build the layout sent to the server, or return an error message.
  function buildLayout() {
    if (mode() === "single") {
      const id = $("rec-device").value;
      if (!id) return { error: "Choose a microphone." };
      return { layout: [{ stream: "room", device_id: id, device_label: label(id), channels: 1, names: ["Room"] }] };
    }
    const layout = [], seen = new Set(), names = new Set();
    for (let i = 0; i < receivers.length; i++) {
      const r = receivers[i];
      if (!r.deviceId) return { error: `Choose a device for receiver ${i + 1}.` };
      if (seen.has(r.deviceId)) return { error: `Receiver ${i + 1} uses the same device as another receiver.` };
      seen.add(r.deviceId);
      const n = effectiveNames(r, i);
      for (const x of n) {
        if (!x) continue;
        if (!NAME_RE.test(x)) return { error: `Player name "${x}": use letters, digits, _ . - (no spaces).` };
        if (names.has(x.toLowerCase())) return { error: `Player name "${x}" is used twice.` };
        names.add(x.toLowerCase());
      }
      if (n.every((x) => !x)) continue;   // receiver with every channel unused: skip it
      layout.push({ stream: `rx${i + 1}`, device_id: r.deviceId, device_label: label(r.deviceId), channels: r.channels || 2, names: n });
    }
    if (!names.size) return { error: "At least one channel needs a player." };
    return { layout };
  }

  function updateButtons() {
    const ok = permitted && !buildLayout().error && !live && !restarting;
    $("rec-start").disabled = !ok || !$("rec-resume").hidden;
  }

  // ------------------------------------------------------------ recording
  let live = null;          // { sess, part, recs: [{ stream, layout, mr, media, seq, mime, startedAt, lastData, saving }] }
  let stopping = false, restarting = false, clockTimer = null, wakeLock = null, current = null;

  function pickMime() {
    const types = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/webm"];
    return types.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
  }

  async function startPart(sess, reason) {
    // Open every mic first; start the recorders together only when all of them are available.
    const media = [];
    try {
      for (const s of sess.layout) {
        const m = await navigator.mediaDevices.getUserMedia(constraints(s.device_id, sess.mode === "lavs"));
        const got = m.getAudioTracks()[0].getSettings().channelCount || 1;
        if (s.channels === 2 && got < 2) { m.getTracks().forEach((t) => t.stop()); throw new Error(`${s.device_label || s.stream} now delivers only one channel`); }
        media.push(m);
      }
    } catch (e) {
      media.forEach((m) => m.getTracks().forEach((t) => t.stop()));
      throw e;
    }
    const part = (sess.part || 0) + 1;
    const mime = pickMime();
    const L = { sess, part, recs: [] };
    sess.layout.forEach((s, i) => {
      const mr = new MediaRecorder(media[i], Object.assign(mime ? { mimeType: mime } : {}, { audioBitsPerSecond: BITRATE[s.channels] || 96000 }));
      L.recs.push({ stream: s.stream, layout: s, mr, media: media[i], seq: 0, mime: mr.mimeType || mime || "audio/webm", startedAt: 0, lastData: Date.now(), saving: Promise.resolve() });
    });
    sess.part = part;
    sess.parts = sess.parts || {};
    sess.parts[part] = { reason, streams: {} };
    for (const R of L.recs) {
      R.mr.ondataavailable = (e) => {
        if (!e.data || !e.data.size) return;
        const seq = ++R.seq;
        R.lastData = Date.now();
        R.saving = R.saving.then(async () => {
          const c = { rid: sess.rid, part, stream: R.stream, seq, mime: R.mime, started_at_ms: R.startedAt, blob: e.data };
          try { await putChunk(c); } catch (err) {
            memQueue.push(c);   // not lost: sent straight to the server instead
            setError(`Couldn't save audio in this browser (${err.message || err.name}; storage full?). Sending it straight to the server instead: keep the connection up and free some disk space.`);
          }
          sess.parts[part].streams[R.stream].chunks = seq;
          try { await putSession(sess); } catch (_) { /* the in-memory session still has the counts */ }
          kick();
        });
      };
      R.mr.onerror = (e) => deviceProblem(L, `${R.layout.device_label || R.stream}: recorder error ${e.error ? e.error.message : ""}`);
      R.media.getAudioTracks().forEach((t) => { t.onended = () => deviceProblem(L, `${R.layout.device_label || R.stream} disconnected`); });
    }
    // Start all recorders back to back; each start time goes to the server for lining the mics up.
    for (const R of L.recs) { R.startedAt = Date.now(); R.lastData = R.startedAt; R.mr.start(TIMESLICE_MS); }
    for (const R of L.recs) sess.parts[part].streams[R.stream] = { started_at_ms: R.startedAt, mime: R.mime, chunks: 0 };
    sess.first_started_ms = sess.first_started_ms || L.recs[0].startedAt;
    sess.status = "recording";
    await putSession(sess);
    live = L;
    meterStreams(L.recs.map((R) => ({ key: R.stream === "room" ? "room" : R.stream, stream: R.media })));
    return L;
  }

  function stopRec(R) {
    return new Promise((resolve) => {
      const done = () => { R.media.getTracks().forEach((t) => { t.onended = null; t.stop(); }); R.saving.then(resolve, resolve); };
      if (R.mr.state === "inactive") return done();
      R.mr.onstop = done;
      try { R.mr.stop(); } catch (_) { done(); }
    });
  }
  const endPart = (L) => (L ? Promise.all(L.recs.map(stopRec)) : Promise.resolve());

  async function deviceProblem(L, why) {
    if (stopping || restarting || live !== L) return;
    restarting = true;
    beep();
    setError(`Recording problem: ${why}. Restarting all microphones as a new part...`);
    await endPart(L);
    live = null;
    stopMeters();
    while (!stopping) {
      try {
        await startPart(L.sess, "restarted after: " + why);
        setError("");
        setState(`Recording (part ${live.part}, restarted after: ${why})`);
        beep();
        if (L.sess.mode === "lavs") clapPrompt();
        break;
      } catch (e) {
        setState(`Microphone unavailable (${e.message}). Retrying every ${RETRY_DEVICE_MS / 1000} s; press Stop to end.`);
        await sleep(RETRY_DEVICE_MS);
      }
    }
    restarting = false;
  }

  // Stall watchdog: a mic that stops producing chunks restarts the part.
  setInterval(() => {
    if (!live || stopping || restarting) return;
    const stalled = live.recs.find((R) => Date.now() - R.lastData > STALL_MS);
    if (stalled) deviceProblem(live, `no audio from ${stalled.layout.device_label || stalled.stream} for 20 s`);
  }, 5000);

  async function holdWakeLock() {
    try { if ("wakeLock" in navigator && !wakeLock) { wakeLock = await navigator.wakeLock.request("screen"); wakeLock.onrelease = () => { wakeLock = null; }; } } catch (_) { /* not allowed */ }
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && live) holdWakeLock(); });
  // Warn before leaving while recording, while finishing, or while audio hasn't reached the server.
  // (Browsers show their own generic "Leave site?" text; the page explains the details itself.)
  window.addEventListener("beforeunload", (e) => {
    if (live || restarting || finishing || memQueue.length || lastPending > 0) { e.preventDefault(); e.returnValue = ""; }
  });

  function tickClock() {
    clearInterval(clockTimer);
    clockTimer = setInterval(() => {
      if (current && current.first_started_ms) $("rec-clock").textContent = hms(Date.now() - current.first_started_ms);
      if (live && !restarting) setState(`Recording (part ${live.part}) · ${live.recs.map((R) => R.seq).reduce((a, b) => a + b, 0)} chunk(s) saved · ${live.recs.length} mic(s)`);
    }, 500);
  }

  function readOptions() {
    const pub = $("rec-publish");   // absent for accounts that keep results on the server
    const opts = { publish: pub ? pub.checked : false };
    const dz = $("rec-diarize");
    if (mode() === "single" && dz && dz.checked) {
      opts.diarize = true;
      const n = parseInt($("rec-people").value, 10);
      if (n > 0) { opts.min_speakers = n; opts.max_speakers = n; }
    }
    return opts;
  }

  function lockSetup(on) {
    $("rec-setup").classList.toggle("locked", on);
    $("rec-start").hidden = on; $("rec-stop").hidden = !on;
    $("rec-keep-open").hidden = !on;
  }

  async function start(resumeSess) {
    setError("");
    if (!permitted) { await $("rec-allow").onclick(); if (!permitted) return; }
    let sess = resumeSess;
    if (!sess) {
      const b = buildLayout();
      if (b.error) { setError(b.error); return; }
      const date = $("rec-date").value;
      sess = { rid: newId(), mode: mode(), layout: b.layout, clap: $("rec-clap").value, session_date: date,
        title: $("rec-title").value.trim() || `Session ${date}`, options: readOptions(), part: 0, parts: {},
        status: "recording", created: Date.now() };
    } else {
      // Devices can change id after a reboot; re-match by name when the saved id is gone.
      for (const s of sess.layout) {
        if (!deviceList.some((d) => d.deviceId === s.device_id)) {
          const byName = deviceList.find((d) => d.label === s.device_label);
          if (byName) s.device_id = byName.deviceId;
        }
      }
    }
    stopPreview();
    current = sess;
    stopping = false;
    try {
      await startPart(sess, resumeSess ? "resumed" : "start");
    } catch (e) {
      setError("Could not start recording: " + e.message);
      current = resumeSess || null;
      preview();
      return;
    }
    registered.delete(sess.rid);
    kick();
    holdWakeLock();
    tickClock();
    $("rec-resume").hidden = true;
    lockSetup(true);
    beep();
    if (sess.mode === "lavs") clapPrompt();
  }

  // Big "after Stop" panel: which step we're on, a progress bar, and whether the page may be closed.
  let finishing = false, stepTimer = null;
  const STEPS = ["Stopping the microphones", "Uploading the last of the audio", "Building the tracks on the server", "Opening the job"];
  function finishStep(i, pct, detail) {
    const box = $("rec-finish");
    if (box.hidden) { box.hidden = false; box.scrollIntoView({ behavior: "smooth", block: "center" }); }
    $("rec-finish-steps").innerHTML = STEPS.map((t, k) =>
      `<li class="${k < i ? "done" : k === i ? "now" : ""}">${k < i ? "&#10003; " : ""}${esc(t)}${k === i && detail ? ` <span class="muted">· ${esc(detail)}</span>` : ""}</li>`).join("");
    const bar = $("rec-finish-bar");
    bar.parentElement.classList.toggle("indeterminate", pct == null);
    bar.style.width = pct == null ? "35%" : Math.max(2, Math.min(100, pct)).toFixed(0) + "%";
    $("rec-finish-title").textContent = i >= 3 ? "Done. You can close this page." : "Finishing. Don't close this page yet.";
    $("rec-finish-title").className = i >= 3 ? "finish-title ok" : "finish-title";
    $("rec-finish-note").textContent = i >= 3 ? "The job runs on the server now and publishes to Nextcloud when it's done."
      : "If it does get closed, nothing is lost: open the Record page again and choose Finish & process.";
  }
  function stepTimerStart(i, label) {
    const t0 = Date.now();
    clearInterval(stepTimer);
    stepTimer = setInterval(() => finishStep(i, null, `${label} ${Math.round((Date.now() - t0) / 1000)} s`), 1000);
  }

  async function stop() {
    if (!current || !confirm("Stop recording?")) return;
    stopping = true;
    finishing = true;
    $("rec-stop").disabled = true;
    setState("Stopping...");
    $("rec-keep-open").hidden = true;
    finishStep(0, 5);
    const L = live;
    live = null;
    await endPart(L);
    stopMeters();
    current.status = "stopped";
    await putSession(current);
    if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
    clearInterval(clockTimer);
    await finishSession(current);
  }

  async function finishSession(sess) {
    finishing = true;
    $("rec-start").hidden = true; $("rec-stop").hidden = true;
    setState("Uploading the rest of the audio...");
    registered.delete(sess.rid);
    kick();
    const total = Math.max(1, (await pendingInfo()).byRid[sess.rid] || 0);
    for (;;) {
      const left = (await pendingInfo()).byRid[sess.rid] || 0;
      finishStep(1, 10 + 60 * (total - left) / total, left ? `${left} chunk(s) to go${lastUploadError ? " (waiting for the connection)" : ""}` : "done");
      if (!left) break;
      kick();
      await sleep(1000);
    }
    const chunks = {};
    for (const [p, info] of Object.entries(sess.parts || {})) {
      for (const [s, si] of Object.entries(info.streams || {})) if (si.chunks) (chunks[p] = chunks[p] || {})[s] = si.chunks;
    }
    for (let attempt = 1; ; attempt++) {
      try {
        if (!registered.has(sess.rid)) await register(sess);
        setState("Building the recording on the server and queuing it for transcription...");
        finishStep(2, null, "joining and lining up the tracks");
        stepTimerStart(2, "joining and lining up the tracks,");
        const job = await call("POST", `/api/v1/recordings/${sess.rid}/finish`, { chunks });
        clearInterval(stepTimer);
        await deleteLocal(sess.rid);
        current = null;
        finishing = false;
        lastPending = 0;
        finishStep(3, 100, `job ${job.id}`);
        setState("Done");
        await sleep(1200);
        location.href = `/jobs/${job.id}`;
        return;
      } catch (e) {
        const fatal = (e.status && e.status < 500 && ![401, 408, 409, 429].includes(e.status)) || (e.status === 500 && attempt >= 3);
        clearInterval(stepTimer);
        if (fatal) {
          setError(`Finishing failed: ${e.message}. The audio is kept; reload the page to try again or discard it.`);
          setState("Stopped, not finished");
          finishing = false;
          $("rec-finish-title").textContent = "Finishing failed. The audio is kept.";
          $("rec-finish-title").className = "finish-title err-text";
          $("rec-finish-bar").parentElement.className = "bar big failed";
          return;
        }
        setState(`Waiting to finish (${e.message}); retrying...`);
        finishStep(2, null, `server not ready (${e.message}), retrying`);
        await sleep(Math.min(30000, 2000 * attempt));
        kick();
        await waitForUpload(sess.rid);
      }
    }
  }

  async function discard(rid) {
    if (!confirm("Discard this recording? Audio that hasn't been processed is deleted from this browser and the server.")) return;
    await deleteLocal(rid);
    try { await call("DELETE", `/api/v1/recordings/${rid}`); } catch (_) { /* not on server / offline */ }
    location.reload();
  }

  // ------------------------------------------------------------ unfinished recordings
  const playersOf = (s) => s.layout.flatMap((x) => x.names.filter(Boolean)).join(", ");
  async function showUnfinished() {
    const sessions = (await getSessions()).sort((a, b) => b.created - a.created);
    const p = await pendingInfo();
    const s = sessions[0];
    if (s) {
      current = s;
      const parts = Object.keys(s.parts || {}).length;
      const started = s.first_started_ms ? new Date(s.first_started_ms).toLocaleString() : "?";
      const waiting = p.byRid[s.rid] || 0;
      $("rec-resume-text").textContent = (s.status === "recording" ? "A recording was interrupted" : "A recording was stopped but not finished") +
        `: "${s.title}" (${s.mode === "lavs" ? "lavs: " + playersOf(s) : "room mic"}), started ${started}, ${parts} part(s)` +
        (waiting ? `, ${waiting} chunk(s) still to upload (uploading now).` : ", all audio uploaded.");
      $("rec-resume").hidden = false;
      $("rec-resume-btn").onclick = () => start(s);
      $("rec-resume-finish").onclick = () => { $("rec-resume").hidden = true; finishSession(s); };
      $("rec-resume-discard").onclick = () => discard(s.rid);
    }
    updateButtons();
    try {
      const local = new Set(sessions.map((x) => x.rid));
      const rows = (await call("GET", "/api/v1/recordings")).filter((r) => !local.has(r.id));
      $("rec-server").hidden = rows.length === 0;
      $("rec-server-rows").innerHTML = rows.map((r) => `<tr><td>${esc(r.title)}</td><td>${esc(r.session_date)}</td>
        <td class="small">${esc(r.mode === "single" ? "room mic" : r.players.join(", "))}</td>
        <td class="mono small">${r.parts.length} part(s), ${mb(r.bytes)}</td>
        <td class="small">${r.last_chunk_at ? new Date(r.last_chunk_at * 1000).toLocaleString() : ""}${r.error ? `<div class="err-text">${esc(r.error)}</div>` : ""}</td>
        <td><button class="small" data-fin="${esc(r.id)}">Finish &amp; process</button> <button class="small danger" data-del="${esc(r.id)}">Discard</button></td></tr>`).join("");
    } catch (_) { /* offline */ }
  }
  $("rec-server-rows").addEventListener("click", async (e) => {
    const fin = e.target.dataset.fin, del = e.target.dataset.del;
    try {
      if (fin) { e.target.disabled = true; e.target.textContent = "Finishing..."; const job = await call("POST", `/api/v1/recordings/${fin}/finish`, {}); location.href = `/jobs/${job.id}`; }
      if (del && confirm("Discard this recording from the server?")) { await call("DELETE", `/api/v1/recordings/${del}`); location.reload(); }
    } catch (err) { alert(err.message); e.target.disabled = false; e.target.textContent = "Finish & process"; }
  });

  // ------------------------------------------------------------ browser storage
  const gb = (n) => (n / 1073741824).toFixed(n < 10 * 1073741824 ? 1 : 0) + " GB";
  async function checkStorage() {
    const el = $("rec-storage"), warn = $("rec-storage-warn");
    if (!navigator.storage || !navigator.storage.estimate) { el.textContent = ""; return; }
    let persisted = false;
    try {
      persisted = await navigator.storage.persisted();
      if (!persisted && navigator.storage.persist) persisted = await navigator.storage.persist();
    } catch (_) { /* best effort */ }
    let quota = 0, usage = 0;
    try { ({ quota = 0, usage = 0 } = await navigator.storage.estimate()); } catch (_) { return; }
    const free = Math.max(0, quota - usage);
    // Offline growth per hour: about 58 MB per receiver (stereo), 43 MB for a room mic.
    const perHour = mode() === "lavs" ? Math.max(1, receivers.filter((r) => r.deviceId).length) * 58 * 1048576 : 43 * 1048576;
    const hours = free / perHour;
    el.textContent = `Browser storage: ${gb(free)} free for this site, enough for about ${hours > 99 ? "100+" : Math.floor(hours)} hour(s) of recording without a connection; ` +
      (persisted ? "kept until the recording is uploaded." : "not marked persistent (Chrome only clears it if the disk is nearly full).");
    const lowQuota = quota < 2 * 1073741824;
    warn.hidden = !lowQuota && hours >= 2;
    warn.textContent = lowQuota
      ? `Only ${gb(quota)} of browser storage is available. This looks like a private (Incognito) window, where everything is deleted when the window closes. Record in a normal window.`
      : `Browser storage is nearly full (${gb(free)} free). Free some disk space before a long session.`;
  }

  // ------------------------------------------------------------ init
  async function init() {
    if (!window.isSecureContext) {
      const u = $("rec-unsupported"); u.hidden = false;
      u.textContent = "Recording needs HTTPS. Open this page through your https:// address (Nginx Proxy Manager), not http://.";
      return;
    }
    if (navigator.mediaDevices && micFunctionReplaced()) showHooked("the browser's microphone function has been replaced");
    if (!navigator.mediaDevices || !window.MediaRecorder || !window.indexedDB) {
      const u = $("rec-unsupported"); u.hidden = false;
      u.textContent = "This browser can't record here (needs MediaRecorder and IndexedDB). Use a current Chrome or Edge.";
      return;
    }
    await checkStorage();
    setInterval(checkStorage, 60000);
    if (cfg.mode === "single") document.querySelector('input[name="rec-mode"][value="single"]').checked = true;
    if (cfg.clap) $("rec-clap").value = cfg.clap;
    $("rec-processing").checked = !!cfg.processing;
    $("rec-lavs").hidden = mode() !== "lavs";
    $("rec-single").hidden = mode() !== "single";
    $("rec-allow").onclick = async () => {
      try {
        // Keep one stream open while listing: with a one-time grant, Chrome hides the mics again once all stop.
        // Unprocessed and stereo, like the recording: a processed capture of the same device can make
        // Chrome merge that receiver's channels for every other use on this page.
        const s = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: { ideal: 2 }, echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
        await listDevices();
        s.getTracks().forEach((t) => t.stop());
      } catch (e) { setError("Microphone access was refused: " + e.message); }
    };
    $("rec-refresh").onclick = () => listDevices();
    $("rec-diag").onclick = async () => {
      const t = diagText();
      try { await navigator.clipboard.writeText(t); $("rec-diag").textContent = "Copied"; } catch (_) { prompt("Copy this:", t); }
      setTimeout(() => { $("rec-diag").textContent = "Copy diagnostics"; }, 2000);
    };
    $("rec-device").onchange = () => { persist(); preview(); };
    $("rec-processing").onchange = () => { persist(); preview(); };
    $("rec-clap").onchange = persist;
    navigator.mediaDevices.addEventListener("devicechange", () => { if (!live && !restarting) listDevices(); });
    $("rec-start").onclick = () => start(null);
    $("rec-stop").onclick = stop;
    renderReceivers();
    await listDevices();
    await showUnfinished();
    renderUpload();
    kick();
  }

  if (navigator.locks && navigator.locks.request) {
    navigator.locks.request("ttrpg-recorder", { ifAvailable: true }, async (lock) => {
      if (!lock) { $("rec-other-tab").hidden = false; $("rec-setup").hidden = true; $("rec-panel").hidden = true; return; }
      await init();
      await new Promise(() => {});   // hold the lock while this tab is open
    });
  } else {
    init();
  }
})();
