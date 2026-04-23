"""Nginx config management — writes/removes site configs and reloads nginx."""
import os
import subprocess
from pathlib import Path

SITES_AVAILABLE = Path("/etc/nginx/sites-available")
SITES_ENABLED = Path("/etc/nginx/sites-enabled")


def _config_name(website_id: int) -> str:
    return f"panel_site_{website_id}"


def generate_config(
    domain: str,
    proxy_pass: str = "",
    extra_config: str = "",
    ssl: bool = False,
    extra_domains: list[str] | None = None,
    mode: str = "proxy",
    listen_port: int = 80,
    webroot: str = "",
) -> str:
    """Build nginx server block.

    mode="proxy"  → proxy_pass to backend
    mode="static" → serve files from `webroot` with try_files SPA fallback.
    """
    all_domains = [domain] + [d for d in (extra_domains or []) if d and d != domain]
    server_name = " ".join(all_domains)

    listen_lines = [f"listen {listen_port};"]
    if ssl:
        listen_lines.append("listen 443 ssl;")
    listen_block = "\n    ".join(listen_lines)

    ssl_block = ""
    if ssl:
        ssl_block = f"""
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
"""

    extra = (extra_config or "").strip()
    extra_block = f"\n    # Custom config\n    {extra}\n" if extra else ""

    if mode == "static":
        location_block = f"""
    root {webroot};
    index index.html index.htm;

    location / {{
        try_files $uri $uri/ /index.html;
    }}
"""
    else:
        target = proxy_pass or "http://127.0.0.1:80"
        location_block = f"""
    location / {{
        proxy_pass {target};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }}
"""

    log_tag = domain.replace("/", "_")
    config = f"""server {{
    {listen_block}
    server_name {server_name};
{ssl_block}
    access_log /var/log/nginx/panel_{log_tag}.access.log;
    error_log  /var/log/nginx/panel_{log_tag}.error.log;
{extra_block}{location_block}
}}
"""
    return config


def write_config(website_id: int, config: str) -> bool:
    """Write config to sites-available. Returns True on success."""
    try:
        SITES_AVAILABLE.mkdir(parents=True, exist_ok=True)
        path = SITES_AVAILABLE / _config_name(website_id)
        path.write_text(config)
        return True
    except Exception as e:
        raise RuntimeError(f"Failed to write nginx config: {e}")


def enable_site(website_id: int) -> bool:
    """Symlink sites-available → sites-enabled."""
    try:
        SITES_ENABLED.mkdir(parents=True, exist_ok=True)
        src = SITES_AVAILABLE / _config_name(website_id)
        dst = SITES_ENABLED / _config_name(website_id)
        if not src.exists():
            raise RuntimeError("Config file not found in sites-available")
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(src)
        return True
    except Exception as e:
        raise RuntimeError(f"Failed to enable site: {e}")


def disable_site(website_id: int) -> bool:
    """Remove symlink from sites-enabled."""
    try:
        dst = SITES_ENABLED / _config_name(website_id)
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        return True
    except Exception as e:
        raise RuntimeError(f"Failed to disable site: {e}")


def delete_config(website_id: int):
    """Remove config from both sites-available and sites-enabled."""
    disable_site(website_id)
    path = SITES_AVAILABLE / _config_name(website_id)
    if path.exists():
        path.unlink()


def _sudo(cmd: list[str]) -> list[str]:
    """Prepend sudo -n if not running as root."""
    if os.geteuid() == 0:
        return cmd
    return ["sudo", "-n", *cmd]


def test_config() -> tuple[bool, str]:
    """Run nginx -t. Returns (ok, output)."""
    try:
        r = subprocess.run(_sudo(["nginx", "-t"]), capture_output=True, text=True, timeout=10)
        output = (r.stdout + r.stderr).strip()
        return r.returncode == 0, output
    except FileNotFoundError:
        return False, "nginx binary not found"
    except Exception as e:
        return False, str(e)


def reload_nginx() -> tuple[bool, str]:
    """Reload nginx (applies config without downtime)."""
    ok, msg = test_config()
    if not ok:
        return False, f"Config test failed: {msg}"
    try:
        r = subprocess.run(_sudo(["systemctl", "reload", "nginx"]), capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            # fallback: nginx -s reload
            r2 = subprocess.run(_sudo(["nginx", "-s", "reload"]), capture_output=True, text=True, timeout=10)
            output = (r2.stdout + r2.stderr).strip()
            return r2.returncode == 0, output or "reloaded"
        return True, "nginx reloaded"
    except Exception as e:
        return False, str(e)


def nginx_status() -> dict:
    """Return nginx status info."""
    # Check if running
    try:
        r = subprocess.run(["systemctl", "is-active", "nginx"], capture_output=True, text=True, timeout=5)
        running = r.stdout.strip() == "active"
    except Exception:
        try:
            r2 = subprocess.run(["pgrep", "-x", "nginx"], capture_output=True, timeout=5)
            running = r2.returncode == 0
        except Exception:
            running = False

    ok, test_out = test_config()
    return {"running": running, "config_ok": ok, "config_test_output": test_out}


def issue_ssl(domain: str) -> tuple[bool, str]:
    """Issue Let's Encrypt cert via certbot."""
    try:
        r = subprocess.run(
            _sudo(["certbot", "--nginx", "-d", domain, "--non-interactive", "--agree-tos", "-m", "admin@panel.local"]),
            capture_output=True, text=True, timeout=120,
        )
        output = (r.stdout + r.stderr).strip()
        return r.returncode == 0, output
    except FileNotFoundError:
        return False, "certbot not found — install it: apt install certbot python3-certbot-nginx"
    except Exception as e:
        return False, str(e)
