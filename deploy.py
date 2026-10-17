"""Deploy Radionline_Backend (FastAPI) to the Hostinger VPS.

Usage (PowerShell, from this folder):
  $env:VPS_PASS = "<root password>"   # optional; else FILE_SFTP_PASSWORD from .env, else prompt
  $env:DEPLOY_ENV = "dev"             # or "prod" (default: prod)
  py deploy.py

Optional env vars:
  DEPLOY_ENV      dev | prod (default prod)
  VPS_HOST        default 91.108.105.182
  VPS_USER        default root
  REMOTE_DIR      override server folder
  SERVICE         override systemd unit
  RUN_MIGRATIONS  set to 1 to run migrations/run_migrations.py after install
  WEB_ORIGIN      frontend origin added to CORS_ORIGINS
  API_ORIGIN      used for FILE_PUBLIC_BASE_URL refresh
  HEALTH_URL      local health check URL after restart

What it does: uploads the source (skips .env, .venv, data, caches), copies it
over the server folder (nothing on the server is deleted), installs
requirements into the service's venv, refreshes CORS and file-storage settings
in the server .env (which must already exist), restarts the API service and
checks /api/health. It never touches the frontend.
Needs: pip install paramiko
"""
from __future__ import annotations

import getpass
import io
import os
import shlex
import sys
import tarfile
from pathlib import Path

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
HOST = os.environ.get("VPS_HOST", "91.108.105.182")
USER = os.environ.get("VPS_USER", "root")
DEPLOY_ENV = (os.environ.get("DEPLOY_ENV") or "prod").strip().lower()
RUN_MIGRATIONS = os.environ.get("RUN_MIGRATIONS", "0")

TARGETS = {
    "prod": {
        "remote_dir": "/var/www/radionline/backend",
        "service": "radionline-api",
        "storage_root": "/var/www/radionline/storage",
        "web_origin": "https://radionlineofficial.com",
        "api_origin": "https://radionlineofficial.com",
        "health_url": "http://127.0.0.1:8000/api/health",
    },
    "dev": {
        "remote_dir": "/var/www/radionline-dev/backend",
        "service": "radionline-dev-api",
        "storage_root": "/var/www/radionline-dev/storage",
        "web_origin": "https://dev.radionlineofficial.com",
        "api_origin": "https://dev.radionlineofficial.com",
        "health_url": "http://127.0.0.1:8001/api/health",
    },
}

if DEPLOY_ENV not in TARGETS:
    raise SystemExit(f"DEPLOY_ENV must be one of: {', '.join(TARGETS)} (got {DEPLOY_ENV!r})")

_target = TARGETS[DEPLOY_ENV]
REMOTE_DIR = os.environ.get("REMOTE_DIR", _target["remote_dir"])
SERVICE = os.environ.get("SERVICE", _target["service"])
STORAGE_ROOT = os.environ.get("STORAGE_ROOT", _target["storage_root"])
WEB_ORIGIN = os.environ.get("WEB_ORIGIN", _target["web_origin"])
API_ORIGIN = os.environ.get("API_ORIGIN", _target["api_origin"])
HEALTH_URL = os.environ.get("HEALTH_URL", _target["health_url"])
REMOTE_TGZ = f"/tmp/radionline-backend-{DEPLOY_ENV}.tgz"

SKIP_DIRS = {".git", ".venv", "venv", "env", "ENV", "__pycache__", ".pytest_cache", "data", ".idea", ".vscode"}
SKIP_FILES = {".env", "deploy.py", "_deploy_probe.py", "_deploy_run.py"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log"}


def should_skip(rel: Path) -> bool:
    if set(rel.parts) & SKIP_DIRS:
        return True
    return rel.name in SKIP_FILES or rel.suffix in SKIP_SUFFIXES


def make_archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in ROOT.rglob("*"):
            rel = path.relative_to(ROOT)
            if path.is_file() and not should_skip(rel):
                tar.add(path, arcname=rel.as_posix())
    return buf.getvalue()


REMOTE_SCRIPT = r"""
set -euo pipefail
SERVICE=__SERVICE__
DIR=__REMOTE_DIR__
STORAGE_ROOT=__STORAGE_ROOT__
HEALTH_URL=__HEALTH_URL__
if [ -z "$DIR" ]; then DIR=$(systemctl show -p WorkingDirectory --value "$SERVICE" 2>/dev/null || true); fi
if [ -z "$DIR" ]; then DIR=/var/www/radionline/backend; fi
echo "Deploy env: __DEPLOY_ENV__"
echo "Backend folder on server: $DIR"
echo "Service: $SERVICE"

# Safety: never deploy into a frontend or old combined-repo folder.
if [ -f "$DIR/package.json" ] || [ -d "$DIR/frontend" ]; then
  echo "Refusing: $DIR looks like a frontend or combined-repo folder. Set REMOTE_DIR." >&2
  exit 1
fi
if [ ! -f "$DIR/.env" ]; then
  echo "Missing $DIR/.env. Create it on the server first (see setup_vps_env.sh)." >&2
  exit 1
fi

mkdir -p "$DIR" "$STORAGE_ROOT"
chmod 750 "$STORAGE_ROOT"
tar -xzf __TGZ__ -C "$DIR"
rm -f __TGZ__
find "$DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Use the venv the service already runs from, else $DIR/.venv
UVICORN=$(systemctl show -p ExecStart --value "$SERVICE" 2>/dev/null | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -1)
if [ -n "$UVICORN" ] && [ -x "$UVICORN" ]; then VENV=$(dirname "$(dirname "$UVICORN")"); else VENV="$DIR/.venv"; fi
echo "Python venv: $VENV"
if [ ! -x "$VENV/bin/python" ]; then python3 -m venv "$VENV"; fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$DIR/requirements.txt"

ENV_FILE="$DIR/.env" WEB_ORIGIN=__WEB_ORIGIN__ API_ORIGIN=__API_ORIGIN__ STORAGE_ROOT="$STORAGE_ROOT" "$VENV/bin/python" - <<'PY'
import os
from pathlib import Path
p = Path(os.environ["ENV_FILE"])
web, api, storage = os.environ["WEB_ORIGIN"], os.environ["API_ORIGIN"], os.environ["STORAGE_ROOT"]
wanted = {"FILE_STORAGE_ROOT": storage, "FILE_PUBLIC_BASE_URL": api}
out, seen = [], set()
for line in p.read_text().splitlines():
    key = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
    if key in wanted:
        out.append(f"{key}={wanted[key]}"); seen.add(key)
    elif key == "CORS_ORIGINS":
        origins = [o.strip() for o in line.split("=", 1)[1].split(",") if o.strip()]
        for extra in ("http://localhost:3000", "http://127.0.0.1:3000", web):
            if extra not in origins:
                origins.append(extra)
        out.append("CORS_ORIGINS=" + ",".join(origins)); seen.add(key)
    else:
        out.append(line)
for key, val in wanted.items():
    if key not in seen:
        out.append(f"{key}={val}")
if "CORS_ORIGINS" not in seen:
    out.append(f"CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000,{web}")
p.write_text("\n".join(out) + "\n")
print("Refreshed CORS_ORIGINS / FILE_STORAGE_ROOT / FILE_PUBLIC_BASE_URL in .env")
PY

if [ __RUN_MIGRATIONS__ = 1 ]; then
  echo "Running migrations..."
  (cd "$DIR" && set -a && . ./.env && set +a && PYTHONPATH="$DIR" "$VENV/bin/python" migrations/run_migrations.py)
fi

if ! systemctl cat "$SERVICE" >/dev/null 2>&1; then
  echo "Missing systemd unit $SERVICE — create it before deploy." >&2
  exit 1
fi
systemctl restart "$SERVICE"
systemctl is-active "$SERVICE"
# Uvicorn can take several seconds to bind after DB/table bootstrap.
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "$HEALTH_URL"; then
    echo
    exit 0
  fi
  sleep 2
done
echo "Health check failed: $HEALTH_URL" >&2
journalctl -u "$SERVICE" -n 30 --no-pager || true
exit 1
"""


def build_script() -> str:
    return (
        REMOTE_SCRIPT.replace("__REMOTE_DIR__", shlex.quote(REMOTE_DIR))
        .replace("__SERVICE__", shlex.quote(SERVICE))
        .replace("__STORAGE_ROOT__", shlex.quote(STORAGE_ROOT))
        .replace("__WEB_ORIGIN__", shlex.quote(WEB_ORIGIN))
        .replace("__API_ORIGIN__", shlex.quote(API_ORIGIN))
        .replace("__HEALTH_URL__", shlex.quote(HEALTH_URL))
        .replace("__DEPLOY_ENV__", shlex.quote(DEPLOY_ENV))
        .replace("__RUN_MIGRATIONS__", "1" if RUN_MIGRATIONS == "1" else "0")
        .replace("__TGZ__", REMOTE_TGZ)
    )


def run(client: paramiko.SSHClient, command: str, timeout: int = 1800) -> int:
    _stdin, stdout, _stderr = client.exec_command(command, get_pty=True, timeout=timeout)
    for line in iter(stdout.readline, ""):
        print(line.rstrip("\n"), flush=True)
    return stdout.channel.recv_exit_status()


def resolve_password() -> str:
    if os.environ.get("VPS_PASS"):
        return os.environ["VPS_PASS"]
    env_path = ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("FILE_SFTP_PASSWORD="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    print("Using FILE_SFTP_PASSWORD from .env for VPS login.", flush=True)
                    return val
    return getpass.getpass(f"{USER} password for {HOST}: ")


def main() -> None:
    password = resolve_password()
    print(f"Packing backend for DEPLOY_ENV={DEPLOY_ENV}...", flush=True)
    payload = make_archive()
    print(f"Archive {len(payload) // 1024} KB → {SERVICE} @ {REMOTE_DIR}", flush=True)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {HOST}...", flush=True)
    client.connect(HOST, username=USER, password=password, timeout=30, allow_agent=False, look_for_keys=False)
    sftp = client.open_sftp()
    with sftp.file(REMOTE_TGZ, "wb") as remote:
        remote.write(payload)
    sftp.close()

    code = run(client, "bash -s <<'RN_DEPLOY_EOF'\n" + build_script() + "\nRN_DEPLOY_EOF")
    client.close()
    if code != 0:
        print(f"\nDeploy FAILED (exit {code}).", flush=True)
        raise SystemExit(code)
    print(f"\nBackend deployed ({DEPLOY_ENV}): {HEALTH_URL}", flush=True)


if __name__ == "__main__":
    main()
