#!/usr/bin/env bash
# Run ON the VPS as root (after SSH): bash setup_vps_env.sh
set -euo pipefail

APP_ROOT=/var/www/radionline
REPO_URL="${REPO_URL:-https://github.com/valadevanshh/Radionlineofficial.git}"
DB_URL='postgresql://radionline:root@127.0.0.1:5432/radionline'
JWT_SECRET="${JWT_SECRET:-NGKO7hMe-8AGDkstUCHJYH3i9Nu3yZB02bv8mxj8q31PTSzpTQD5q12l5yufTzar}"

mkdir -p "$APP_ROOT/storage"
chmod 750 "$APP_ROOT/storage"

if [ ! -d "$APP_ROOT/app/.git" ] && [ ! -d "$APP_ROOT/app" ]; then
  git clone "$REPO_URL" "$APP_ROOT/app"
elif [ -d "$APP_ROOT/app/.git" ]; then
  git -C "$APP_ROOT/app" pull --ff-only || true
fi

mkdir -p "$APP_ROOT/backend"
cat > "$APP_ROOT/backend/.env" <<EOF
DATABASE_URL=${DB_URL}
ENVIRONMENT=production
ALLOW_SQLITE_FALLBACK=false
JWT_SECRET=${JWT_SECRET}
FILE_STORAGE_ROOT=${APP_ROOT}/storage
FILE_PUBLIC_BASE_URL=
EOF

chmod 600 "$APP_ROOT/backend/.env"

echo "Wrote $APP_ROOT/backend/.env"
echo "Storage: $APP_ROOT/storage"
echo
echo "Next:"
echo "  cd $APP_ROOT/backend"
echo "  python3 -m venv .venv && source .venv/bin/activate"
echo "  pip install -r requirements.txt"
echo "  python migrations/run_migrations.py"
echo "  python run.py"
