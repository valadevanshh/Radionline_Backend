#!/usr/bin/env bash
# ONE-SHOT: paste into VPS as root → Azure-style remote Postgres + app env + migrations
# Usage: bash -s <<'SCRIPT'   OR copy whole file and: bash vps_azure_style_postgres.sh
set -euo pipefail

DB_NAME=radionline
DB_USER=radionline
# Stronger than "root" — change anytime with ALTER USER
DB_PASS='Radionline@2026!'
# URL-encoded for connection strings (@ → %40, ! → %21)
DB_PASS_URL='Radionline%402026%21'
APP_ROOT=/var/www/radionline
REPO_URL='https://github.com/valadevanshh/Radionlineofficial.git'
PUBLIC_IP="$(curl -4 -s ifconfig.me || hostname -I | awk '{print $1}')"
JWT_SECRET='NGKO7hMe-8AGDkstUCHJYH3i9Nu3yZB02bv8mxj8q31PTSzpTQD5q12l5yufTzar'

echo "==> Public IP detected: ${PUBLIC_IP}"

export DEBIAN_FRONTEND=noninteractive
apt update -y
apt install -y postgresql postgresql-contrib git python3-venv python3-pip ufw curl

systemctl enable --now postgresql

echo "==> Ensuring DB role + database"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  ELSE
    ALTER ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}';
  END IF;
END
\$\$;
SELECT 'CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${DB_NAME}')\gexec
\c ${DB_NAME}
GRANT ALL ON SCHEMA public TO ${DB_USER};
ALTER SCHEMA public OWNER TO ${DB_USER};
SQL

PG_VER="$(ls /etc/postgresql | sort -V | tail -1)"
PG_CONF="/etc/postgresql/${PG_VER}/main/postgresql.conf"
PG_HBA="/etc/postgresql/${PG_VER}/main/pg_hba.conf"

echo "==> Configuring Postgres ${PG_VER} for remote connections (Azure-style)"
sed -i "s/^#\?listen_addresses.*/listen_addresses = '*'/" "$PG_CONF"
if ! grep -q "radionline remote" "$PG_HBA"; then
  cat >> "$PG_HBA" <<EOF

# radionline remote (Azure-style)
host    ${DB_NAME}    ${DB_USER}    0.0.0.0/0    scram-sha-256
host    ${DB_NAME}    ${DB_USER}    ::/0         scram-sha-256
EOF
fi

systemctl restart postgresql

echo "==> Firewall: allow 22 + 5432"
ufw allow OpenSSH || true
ufw allow 5432/tcp || true
ufw --force enable || true

echo "==> App directories + clone"
mkdir -p "${APP_ROOT}/storage"
chmod 750 "${APP_ROOT}/storage"
if [ ! -d "${APP_ROOT}/app/.git" ]; then
  rm -rf "${APP_ROOT}/app"
  git clone "${REPO_URL}" "${APP_ROOT}/app"
else
  git -C "${APP_ROOT}/app" pull --ff-only || true
fi

echo "==> Writing backend .env (on-server uses localhost)"
cat > "${APP_ROOT}/backend/.env" <<EOF
DATABASE_URL=postgresql://${DB_USER}:${DB_PASS_URL}@127.0.0.1:5432/${DB_NAME}
ENVIRONMENT=production
ALLOW_SQLITE_FALLBACK=false
JWT_SECRET=${JWT_SECRET}
FILE_STORAGE_ROOT=${APP_ROOT}/storage
FILE_PUBLIC_BASE_URL=
EOF
chmod 600 "${APP_ROOT}/backend/.env"

# Also write remote connection snippet for Windows / Azure-style clients
cat > "${APP_ROOT}/REMOTE_DATABASE_URL.txt" <<EOF
# Use this from Windows / other machines (like Azure connection string):
DATABASE_URL=postgresql://${DB_USER}:${DB_PASS_URL}@${PUBLIC_IP}:5432/${DB_NAME}

# Plain password (for reference): ${DB_PASS}
# Hostinger panel: also open TCP 5432 in VPS Firewall if present.
EOF
chmod 600 "${APP_ROOT}/REMOTE_DATABASE_URL.txt"

echo "==> Python venv + migrations"
cd "${APP_ROOT}/backend"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python migrations/run_migrations.py

echo
echo "============================================"
echo " DONE"
echo " On-server DATABASE_URL:"
echo "   postgresql://${DB_USER}:${DB_PASS_URL}@127.0.0.1:5432/${DB_NAME}"
echo " Remote (Azure-style) DATABASE_URL:"
echo "   postgresql://${DB_USER}:${DB_PASS_URL}@${PUBLIC_IP}:5432/${DB_NAME}"
echo " Saved to: ${APP_ROOT}/REMOTE_DATABASE_URL.txt"
echo "============================================"
echo "IMPORTANT: In Hostinger hPanel → VPS → Firewall, allow inbound TCP 5432"
echo "Then from Windows test:"
echo "  psql \"postgresql://${DB_USER}:${DB_PASS_URL}@${PUBLIC_IP}:5432/${DB_NAME}\" -c \"SELECT 1;\""
