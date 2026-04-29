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
  patch(p, body) { return this.req(p, { method: "PATCH", body: JSON.stringify(body || {}) }); },
  del(p) { return this.req(p, { method: "DELETE" }); },
};

function toast(msg, type = "info", timeout = 3500) {
  let wrap = document.querySelector(".toast-wrap");
  if (!wrap) { wrap = document.createElement("div"); wrap.className = "toast-wrap"; document.body.appendChild(wrap); }
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  wrap.appendChild(el);
  if (timeout > 0) setTimeout(() => el.remove(), timeout);
  return el;
}

function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(1) + " " + u[i];
}

async function loadSidebar(active) {
  enhanceChrome();
  try {
    const me = await api.get("/api/auth/me");
    const userBox = document.querySelector(".sidebar .user");
    if (userBox) userBox.innerHTML = `<b>${me.username}</b><br>${me.is_admin ? "Administrator" : "User"}`;
    if (!me.is_admin) {
      const a = document.querySelector('.sidebar a[href="/admin"]');
      if (a) a.style.display = "none";
    }
    try {
      const flags = await api.get("/api/settings/public");
      if (!flags.experimental_websites) {
        const a = document.querySelector('.sidebar a[href="/websites"]');
        if (a) a.style.display = "none";
      }
    } catch (e) {}
    document.querySelectorAll(".sidebar nav a").forEach(a => {
      if (a.getAttribute("href") === active) a.classList.add("active");
    });
    enhanceChrome();
  } catch (e) {}
}

async function logout() {
  await api.post("/api/auth/logout");
  location.href = "/login";
}

const UI_ICON_PATHS = {
  panel: '<path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/>',
  servers: '<rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20"/>',
  settings: '<path d="M12 15.5A3.5 3.5 0 1 0 12 8a3.5 3.5 0 0 0 0 7.5z"/><path d="M19.4 15a1.8 1.8 0 0 0 .36 1.98l.05.05a2.1 2.1 0 1 1-2.97 2.97l-.05-.05a1.8 1.8 0 0 0-1.98-.36 1.8 1.8 0 0 0-1.1 1.66V21a2.1 2.1 0 1 1-4.2 0v-.08a1.8 1.8 0 0 0-1.1-1.66 1.8 1.8 0 0 0-1.98.36l-.05.05a2.1 2.1 0 1 1-2.97-2.97l.05-.05A1.8 1.8 0 0 0 4.6 15a1.8 1.8 0 0 0-1.66-1.1H3a2.1 2.1 0 1 1 0-4.2h.08A1.8 1.8 0 0 0 4.74 8.6a1.8 1.8 0 0 0-.36-1.98l-.05-.05A2.1 2.1 0 1 1 7.3 3.6l.05.05a1.8 1.8 0 0 0 1.98.36A1.8 1.8 0 0 0 10.42 2.35V2a2.1 2.1 0 1 1 4.2 0v.08a1.8 1.8 0 0 0 1.1 1.66 1.8 1.8 0 0 0 1.98-.36l.05-.05a2.1 2.1 0 1 1 2.97 2.97l-.05.05a1.8 1.8 0 0 0-.36 1.98 1.8 1.8 0 0 0 1.66 1.1H22a2.1 2.1 0 1 1 0 4.2h-.08A1.8 1.8 0 0 0 19.4 15z"/>',
  user: '<path d="M20 21a8 8 0 0 0-16 0"/><circle cx="12" cy="7" r="4"/>',
  terminal: '<path d="m4 7 5 5-5 5"/><path d="M12 19h8"/>',
  files: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  backup: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
  schedules: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
  subusers: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  network: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20"/>',
  play: '<polygon points="6 4 20 12 6 20 6 4"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  logs: '<path d="M4 4h16v16H4z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  env: '<path d="M12 3v18"/><path d="M6 8h12"/><path d="M6 16h12"/>',
  nginx: '<path d="M4 7.5 12 3l8 4.5v9L12 21l-8-4.5z"/><path d="M9 9v6M9 9l6 6M15 9v6"/>',
};

function uiIcon(name) {
  const path = UI_ICON_PATHS[name] || UI_ICON_PATHS.files;
  return `<svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
}

function stripDecorativePrefix(text) {
  return String(text || "").replace(/^[^\p{L}\p{N}]+/u, "").trim();
}

function navIconForHref(href) {
  if (href === "/dashboard") return "servers";
  if (href === "/websites") return "globe";
  if (href === "/admin") return "settings";
  if (href === "/profile") return "user";
  return "files";
}

function tabIconFor(tab) {
  return ({
    console: "terminal",
    logs: "logs",
    files: "files",
    backups: "backup",
    schedules: "schedules",
    subusers: "subusers",
    network: "network",
    settings: "settings",
    nginx: "nginx",
    ssl: "lock",
    env: "env",
  })[tab] || "files";
}

function enhanceChrome() {
  const logo = document.querySelector(".sidebar .logo");
  if (logo && !logo.querySelector(".logo-mark")) {
    logo.innerHTML = `<span class="logo-mark">${uiIcon("panel")}</span><span class="logo-text">Panel</span>`;
  }

  document.querySelectorAll(".sidebar nav a").forEach(a => {
    if (a.dataset.enhanced === "1") return;
    const label = stripDecorativePrefix(a.textContent);
    a.innerHTML = `${uiIcon(navIconForHref(a.getAttribute("href")))}<span>${label}</span>`;
    a.dataset.enhanced = "1";
  });

  document.querySelectorAll(".tab").forEach(tab => {
    if (tab.dataset.enhanced === "1") return;
    const label = stripDecorativePrefix(tab.textContent);
    tab.innerHTML = `${uiIcon(tabIconFor(tab.dataset.tab))}<span>${label}</span>`;
    tab.dataset.enhanced = "1";
  });
}

document.addEventListener("DOMContentLoaded", enhanceChrome);
