// Panel client utilities
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
