/* ══════════════════════════════════════════════════════════════
   TA Assistant — front-end logic
   ══════════════════════════════════════════════════════════════ */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const el = {
    chat:      $("chat"),
    welcome:   $("welcome"),
    form:      $("chat-form"),
    input:     $("input"),
    send:      $("btn-send"),
    ingest:    $("btn-ingest"),
    clear:     $("btn-clear"),
    menu:      $("btn-menu"),
    sidebar:   $("sidebar"),
    scrim:     $("scrim"),
    dropzone:  $("dropzone"),
    fileInput: $("file-input"),
    fileList:  $("file-list"),
    statChunks:$("stat-chunks"),
    statFiles: $("stat-files"),
    builtAt:   $("built-at"),
    toastWrap: $("toast-wrap"),
  };

  /** ประวัติบทสนทนา ส่งไปให้ Gemini เพื่อให้ถามต่อเนื่องได้ */
  let history = [];
  let busy = false;

  /* ─────────────────────────── utils ─────────────────────────── */

  const escapeHtml = (s) =>
    s.replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /** markdown ขั้นต่ำ: **bold** `code` รายการ 1. / - และ [1] -> chip อ้างอิง */
  function renderMarkdown(raw) {
    const lines = escapeHtml(raw).split("\n");
    let html = "";
    let list = null; // "ol" | "ul" | null

    const closeList = () => { if (list) { html += `</${list}>`; list = null; } };

    for (const line of lines) {
      const text = line.trim();
      if (!text) { closeList(); continue; }

      const ol = text.match(/^(\d+)[.)]\s+(.*)$/);
      const ul = text.match(/^[-*•]\s+(.*)$/);

      if (ol) {
        if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
        html += `<li>${inline(ol[2])}</li>`;
      } else if (ul) {
        if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
        html += `<li>${inline(ul[1])}</li>`;
      } else {
        closeList();
        html += `<p>${inline(text)}</p>`;
      }
    }
    closeList();
    return html;
  }

  const inline = (s) =>
    s
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\[(\d{1,2})\]/g, '<span class="ref">$1</span>');

  function toast(message, kind = "ok", ms = 4200) {
    const node = document.createElement("div");
    node.className = `toast toast--${kind}`;
    node.textContent = message;
    el.toastWrap.appendChild(node);
    setTimeout(() => {
      node.classList.add("is-out");
      node.addEventListener("animationend", () => node.remove(), { once: true });
    }, ms);
  }

  const scrollDown = () =>
    requestAnimationFrame(() => { el.chat.scrollTop = el.chat.scrollHeight; });

  /* ────────────────────── การวาดข้อความ ────────────────────── */

  function hideWelcome() {
    if (el.welcome && el.welcome.parentNode) el.welcome.remove();
  }

  function addMessage(role, contentHtml, sources) {
    hideWelcome();

    const msg = document.createElement("div");
    msg.className = `msg msg--${role === "user" ? "user" : "bot"}`;

    const avatar = document.createElement("div");
    avatar.className = "msg__avatar";
    avatar.textContent = role === "user" ? "คุณ" : "TA";

    const body = document.createElement("div");
    body.className = "msg__body";

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = contentHtml;
    body.appendChild(bubble);

    if (sources && sources.length) body.appendChild(buildSources(sources));

    msg.append(avatar, body);
    el.chat.appendChild(msg);
    scrollDown();
    return msg;
  }

  function buildSources(sources) {
    const wrap = document.createElement("div");
    wrap.className = "sources";

    const head = document.createElement("div");
    head.className = "sources__head";
    head.textContent = `แหล่งอ้างอิง ${sources.length} รายการ`;
    wrap.appendChild(head);

    for (const s of sources) {
      const card = document.createElement("div");
      card.className = "source";
      card.innerHTML = `
        <div class="source__head">
          <span class="source__ref">${s.ref}</span>
          <span class="source__name">${escapeHtml(s.source)}</span>
          ${s.locator ? `<span class="source__loc">${escapeHtml(s.locator)}</span>` : ""}
          <span class="source__score">${Math.round(s.score * 100)}%</span>
          <svg class="source__chev" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2">
            <path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
        <div class="source__body">${escapeHtml(s.excerpt)}</div>`;
      card
        .querySelector(".source__head")
        .addEventListener("click", () => card.classList.toggle("is-open"));
      wrap.appendChild(card);
    }
    return wrap;
  }

  function addTyping() {
    hideWelcome();
    const msg = document.createElement("div");
    msg.className = "msg msg--bot";
    msg.innerHTML = `
      <div class="msg__avatar">TA</div>
      <div class="msg__body">
        <div class="bubble" style="padding:0">
          <div class="typing"><span></span><span></span><span></span></div>
        </div>
      </div>`;
    el.chat.appendChild(msg);
    scrollDown();
    return msg;
  }

  /* ──────────────────────── ถาม-ตอบ ──────────────────────── */

  /** session หมดอายุหรือถูกถอนสิทธิ์ -> ส่งกลับหน้า login แทนที่จะเงียบหาย */
  function handleAuthError(res) {
    if (res.status === 401) {
      toast("เซสชันหมดอายุ — กำลังพากลับไปหน้าเข้าสู่ระบบ", "warn", 2500);
      setTimeout(() => (location.href = "/login"), 1200);
      return true;
    }
    if (res.status === 403) {
      toast("บัญชีของคุณไม่มีสิทธิ์ทำรายการนี้ (เฉพาะผู้ดูแลระบบ)", "error", 5000);
      return true;
    }
    return false;
  }

  /** ถามถี่เกินโควตา — บอกให้รอ ไม่ใช่ error ที่ผู้ใช้ทำอะไรผิด */
  async function handleRateLimit(res) {
    if (res.status !== 429) return false;
    let msg = "ถามถี่เกินไป รบกวนรอสักครู่แล้วลองใหม่นะครับ";
    try {
      const d = await res.json();
      if (typeof d.detail === "string") msg = d.detail;
    } catch { /* ใช้ข้อความเริ่มต้น */ }
    addMessage("bot", renderMarkdown(msg));
    toast("ถามถี่เกินไป", "warn", 5000);
    return true;
  }

  function setBusy(state) {
    busy = state;
    el.send.disabled = state;
    el.input.disabled = state;
  }

  async function ask(question) {
    if (busy || !question.trim()) return;

    addMessage("user", renderMarkdown(question));
    history.push({ role: "user", content: question });

    setBusy(true);
    const typing = addTyping();

    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, history: history.slice(0, -1) }),
      });

      typing.remove();

      if (handleAuthError(res)) return;
      if (await handleRateLimit(res)) return;

      if (!res.ok) {
        const detail = await res.text();
        addMessage("bot", renderMarkdown(`⚠️ เกิดข้อผิดพลาด (${res.status})\n\n${detail.slice(0, 400)}`));
        toast("เรียก API ไม่สำเร็จ", "error");
        return;
      }

      const data = await res.json();
      addMessage("bot", renderMarkdown(data.answer), data.sources);
      history.push({ role: "assistant", content: data.answer });

      if (data.status === "no_index") toast("ยังไม่มี index — กด “สร้าง Index” ก่อน", "warn");
      if (data.status === "retrieval_only") toast("สรุปคำตอบไม่ได้ชั่วคราว — แสดงข้อความจากเอกสารให้แทน", "warn", 6000);
    } catch (err) {
      typing.remove();
      addMessage("bot", renderMarkdown(`⚠️ เชื่อมต่อเซิร์ฟเวอร์ไม่ได้\n\n\`${err.message}\``));
      toast("เชื่อมต่อเซิร์ฟเวอร์ไม่ได้", "error");
    } finally {
      setBusy(false);
      el.input.focus();
    }
  }

  /* ─────────────────────── index / upload ─────────────────────── */

  function refreshStatus(data) {
    el.statChunks.textContent = data.chunk_count ?? 0;
    el.statFiles.textContent = (data.sources || []).length;
    el.builtAt.textContent = data.built_at ? `อัปเดตล่าสุด: ${data.built_at}` : "";

    el.fileList.innerHTML = "";
    if (!data.sources || !data.sources.length) {
      el.fileList.innerHTML = `<li class="file-list__empty">ยังไม่มีเอกสารในระบบ</li>`;
      return;
    }
    for (const s of data.sources) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="file-list__name" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span>
                      <span class="file-list__count">${s.chunks}</span>`;
      el.fileList.appendChild(li);
    }
  }

  async function loadStatus() {
    try {
      const res = await fetch("/api/status");
      if (res.status === 401) { location.href = "/login"; return; }
      if (res.ok) refreshStatus(await res.json());
    } catch { /* เงียบไว้ — ไม่ใช่ error ที่ผู้ใช้ต้องรู้ */ }
  }

  async function runIngest() {
    if (el.ingest.disabled) return;

    const label = el.ingest.querySelector(".btn__label");
    const spinner = el.ingest.querySelector(".spinner");
    el.ingest.disabled = true;
    label.textContent = "กำลังประมวลผล…";
    spinner.hidden = false;

    try {
      const res = await fetch("/api/ingest", { method: "POST" });
      if (handleAuthError(res)) return;
      const data = await res.json();

      if (res.ok && data.ok) {
        toast(data.message, "ok", 5500);
        if (data.failed?.length) {
          toast(`อ่านไม่ได้ ${data.failed.length} ไฟล์: ${data.failed.map((f) => f.name).join(", ")}`, "warn", 7000);
        }
        if (data.backend === "hashing-fallback") {
          toast("ใช้ embedding สำรอง (hashing) — ตรวจ GEMINI_API_KEY เพื่อคุณภาพที่ดีกว่า", "warn", 7000);
        }
        await loadStatus();
      } else {
        toast(data.message || "สร้าง index ไม่สำเร็จ", "error", 7000);
      }
    } catch (err) {
      toast(`สร้าง index ไม่สำเร็จ: ${err.message}`, "error");
    } finally {
      el.ingest.disabled = false;
      label.textContent = "สร้าง Index";
      spinner.hidden = true;
    }
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;

    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));

    toast(`กำลังอัปโหลด ${files.length} ไฟล์…`, "ok", 2500);
    try {
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      if (handleAuthError(res)) return;
      const data = await res.json();

      if (!res.ok) {
        const rejected = data?.detail?.rejected || [];
        toast(rejected.length ? `ไม่รับไฟล์: ${rejected.map((r) => r.name).join(", ")}` : "อัปโหลดไม่สำเร็จ", "error", 6000);
        return;
      }
      toast(data.message, "ok", 5000);
      if (data.rejected?.length) {
        toast(`ข้ามไป ${data.rejected.length} ไฟล์ (นามสกุลไม่รองรับ)`, "warn", 5000);
      }
    } catch (err) {
      toast(`อัปโหลดไม่สำเร็จ: ${err.message}`, "error");
    }
  }

  /* ───────────────────────── events ───────────────────────── */

  el.form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = el.input.value.trim();
    if (!q) return;
    el.input.value = "";
    autoGrow();
    ask(q);
  });

  // Enter = ส่ง, Shift+Enter = ขึ้นบรรทัดใหม่
  el.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      el.form.requestSubmit();
    }
  });

  function autoGrow() {
    el.input.style.height = "auto";
    el.input.style.height = `${Math.min(el.input.scrollHeight, 168)}px`;
  }
  el.input.addEventListener("input", autoGrow);

  document.querySelectorAll(".suggest").forEach((btn) =>
    btn.addEventListener("click", () => ask(btn.dataset.q)));

  el.clear.addEventListener("click", () => {
    history = [];
    el.chat.querySelectorAll(".msg").forEach((n) => n.remove());
    if (!$("welcome")) location.reload();      // เรียกหน้าจอต้อนรับกลับมา
    closeSidebar();
    toast("ล้างบทสนทนาแล้ว", "ok", 2200);
  });

  // ── อัปโหลดเอกสารและสร้าง Index — มีแค่ตอนล็อกอินเป็น admin ──
  // สมาชิกทั่วไปจะไม่มีปุ่มเหล่านี้ในหน้า จึงต้องเช็คก่อนผูก event
  if (el.ingest) el.ingest.addEventListener("click", runIngest);

  if (el.dropzone && el.fileInput) {
    el.dropzone.addEventListener("click", () => el.fileInput.click());
    el.fileInput.addEventListener("change", (e) => {
      uploadFiles(e.target.files);
      e.target.value = "";
    });
    ["dragenter", "dragover"].forEach((ev) =>
      el.dropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        el.dropzone.classList.add("is-over");
      }));
    ["dragleave", "drop"].forEach((ev) =>
      el.dropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        el.dropzone.classList.remove("is-over");
      }));
    el.dropzone.addEventListener("drop", (e) => uploadFiles(e.dataTransfer?.files));
    // กันเบราว์เซอร์เปิดไฟล์แทนเมื่อวางผิดที่
    ["dragover", "drop"].forEach((ev) =>
      document.addEventListener(ev, (e) => {
        if (!el.dropzone.contains(e.target)) e.preventDefault();
      }));
  }

  // ── sidebar บนมือถือ ──
  const openSidebar  = () => { el.sidebar.classList.add("is-open"); el.scrim.classList.add("is-on"); };
  const closeSidebar = () => { el.sidebar.classList.remove("is-open"); el.scrim.classList.remove("is-on"); };
  el.menu.addEventListener("click", openSidebar);
  el.scrim.addEventListener("click", closeSidebar);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSidebar(); });

  /* ───────────────────────── init ───────────────────────── */
  autoGrow();
  loadStatus();
  el.input.focus();
})();
