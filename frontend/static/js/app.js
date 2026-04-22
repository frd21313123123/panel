// Panel client utilities
// ---------- Task bar ----------
const _taskbar = (() => {
  let el = null;
  let _sid = null;
  let _poll = null;

  function mount() {
    if (el) return;
    el = document.createElement("div");
    el.className = "taskbar";
    document.body.appendChild(el);
  }

  function render(tasks) {
    mount();
    el.innerHTML = tasks.map(t => {
      const barClass = t.status === "done" ? "done" : t.status === "error" ? "error" : t.progress < 5 ? "indeterminate" : "";
      const width = (t.status === "running" && t.progress < 5) ? "40%" : t.progress + "%";
      return `<div class="task-item" id="task-${t.id}">
        <div class="task-title">
          <span>${escHtml(t.title)}</span>
          ${t.status !== "running" ? `<span class="task-close" onclick="taskbar.dismiss('${t.id}')">✕</span>` : ""}
        </div>
        ${t.message ? `<div class="task-msg">${escHtml(t.message)}</div>` : ""}
        <div class="progress-track">
          <div class="progress-bar ${barClass}" style="width:${width}"></div>
        </div>
      </div>`;
    }).join("");
  }

  function escHtml(s) { return String(s||"").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

  function startPolling(sid) {
    _sid = sid;
    if (_poll) return;
    _poll = setInterval(async () => {
      try {
        const tasks = await api.get(`/api/servers/${_sid}/tasks`);
        if (tasks.length) render(tasks);
        else if (el) el.innerHTML = "";
        // auto-remove done/error after 4s
        tasks.forEach(t => {
          if (t.status !== "running") {
            setTimeout(() => dismiss(t.id), 4000);
          }
        });
      } catch (e) {}
    }, 800);
  }

  async function dismiss(id) {
    try { await api.del(`/api/tasks/${id}`); } catch (e) {}
    const item = document.getElementById("task-" + id);
    if (item) item.remove();
  }

  return { startPolling, dismiss, render };
})();

const taskbar = _taskbar;
const api = {
  async req(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      credentials: "include",
    });
    if (res.status === 401) { location.href = "/login"; return; }
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("json") ? await res.json() : await res.text();
    if (!res.ok) throw new Error(data.detail || data || "Request failed");
    return data;
  },
  get(p) { return this.req(p); },
  post(p, body) { return this.req(p, { method: "POST", body: JSON.stringify(body || {}) }); },
  del(p) { return this.req(p, { method: "DELETE" }); },
};

function toast(msg, type = "info") {
  let wrap = document.querySelector(".toast-wrap");
  if (!wrap) { wrap = document.createElement("div"); wrap.className = "toast-wrap"; document.body.appendChild(wrap); }
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(1) + " " + u[i];
}

async function loadSidebar(active) {
  try {
    const me = await api.get("/api/auth/me");
    const userBox = document.querySelector(".sidebar .user");
    if (userBox) userBox.innerHTML = `<b>${me.username}</b><br>${me.is_admin ? "Administrator" : "User"}`;
    if (!me.is_admin) {
      const a = document.querySelector('.sidebar a[href="/admin"]');
      if (a) a.style.display = "none";
    }
    document.querySelectorAll(".sidebar nav a").forEach(a => {
      if (a.getAttribute("href") === active) a.classList.add("active");
    });
  } catch (e) {}
}

async function logout() {
  await api.post("/api/auth/logout");
  location.href = "/login";
}
