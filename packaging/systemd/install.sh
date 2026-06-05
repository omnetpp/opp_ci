#!/usr/bin/env bash
# Idempotent installer for the opp_ci systemd units.
#
# Creates the `opp_ci` system user, installs unit files into
# /etc/systemd/system/, seeds /etc/opp_ci/ with example env files
# (without overwriting existing ones), and ensures /opt/opp_ci has a
# Python venv with opp_ci installed editably.
#
# Defaults to provisioning a local PostgreSQL: installs the package if
# missing, creates an `opp_ci` role and database, and points the env
# file at the Unix socket with peer authentication (no password).
# Pass --no-postgres to skip (e.g., when using a remote database).
#
# Run as root:    sudo packaging/systemd/install.sh
#
# After install, enable the units you actually want on this host:
#   sudo systemctl enable --now opp_ci-serve.service
#   sudo systemctl enable --now opp_ci-worker@default.service
#   sudo systemctl enable opp_ci.target

set -euo pipefail

WITH_POSTGRES=1
for arg in "$@"; do
    case "$arg" in
        --no-postgres) WITH_POSTGRES=0 ;;
        --with-postgres) WITH_POSTGRES=1 ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "install.sh must be run as root (try: sudo $0)" >&2
    exit 1
fi

# Resolve script directory and the repo root (two levels up).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

OPP_CI_USER="opp_ci"
OPP_CI_GROUP="opp_ci"
OPP_CI_DB="opp_ci"
INSTALL_DIR="/opt/opp_ci"
STATE_DIR="/var/lib/opp_ci"
CONFIG_DIR="/etc/opp_ci"
WORKER_CONFIG_DIR="$CONFIG_DIR/workers"
TLS_DIR="$CONFIG_DIR/tls"
SYSTEMD_DIR="/etc/systemd/system"
SERVE_DROPIN_DIR="$SYSTEMD_DIR/opp_ci-serve.service.d"
# Filled in below after Postgres is up (some Ubuntu hosts allocate the
# cluster on 5433 rather than the canonical 5432, e.g. when another
# cluster already occupied 5432 at install time).
POSTGRES_SOCKET_URL=""

echo "==> Creating system user '$OPP_CI_USER' (if missing)"
if ! getent group "$OPP_CI_GROUP" >/dev/null; then
    groupadd --system "$OPP_CI_GROUP"
fi
if ! id -u "$OPP_CI_USER" >/dev/null 2>&1; then
    useradd --system \
        --gid "$OPP_CI_GROUP" \
        --home-dir "$STATE_DIR" \
        --shell /usr/sbin/nologin \
        --comment "opp_ci CI service" \
        "$OPP_CI_USER"
fi

echo "==> Creating directories"
install -d -o root         -g root         -m 0755 "$INSTALL_DIR"
install -d -o root         -g "$OPP_CI_GROUP" -m 0750 "$CONFIG_DIR"
install -d -o "$OPP_CI_USER" -g "$OPP_CI_GROUP" -m 0750 "$WORKER_CONFIG_DIR"
install -d -o "$OPP_CI_USER" -g "$OPP_CI_GROUP" -m 0750 "$STATE_DIR"
# TLS materials directory. Empty by default — operator supplies cert/key
# (Cloudflare Origin Cert, Let's Encrypt copy, or `opp_ci tls-selfsign`).
install -d -o root         -g "$OPP_CI_GROUP" -m 0750 "$TLS_DIR"

echo "==> Syncing source tree to $INSTALL_DIR"
# Use rsync if available, otherwise cp -a. Excludes the venv (rebuilt below)
# and any local sqlite DB the developer may have in their checkout. Keeps
# .git/ so setuptools-scm can derive the version, and so operators can
# `cd /opt/opp_ci && sudo git pull` to upgrade.
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude='.venv/' \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        --exclude='opp_ci.db' \
        --exclude='opp_ci.db-*' \
        "$REPO_ROOT/" "$INSTALL_DIR/"
else
    cp -a "$REPO_ROOT/." "$INSTALL_DIR/"
fi
chown -R root:root "$INSTALL_DIR"

echo "==> Creating Python venv and installing opp_ci (editable)"
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
# Install all extras the service needs at runtime: web (uvicorn/fastapi/jinja2
# for `serve`), client (requests for --remote CLI), podman (yaml/jinja2 for
# podman-isolation jobs). Postgres extra is added by default but is
# innocuous (just psycopg2-binary) even if you point at SQLite or a remote DB.
EXTRAS="web,client,podman,postgres"
"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR[$EXTRAS]"

# The venv needs to be readable & executable by the opp_ci user, but the
# rest of /opt/opp_ci can stay root-owned.
chown -R "$OPP_CI_USER:$OPP_CI_GROUP" "$INSTALL_DIR/.venv"

if [[ "$WITH_POSTGRES" -eq 1 ]]; then
    echo "==> Provisioning local PostgreSQL"
    if ! command -v psql >/dev/null 2>&1; then
        echo "    postgresql not installed; running apt-get install -y postgresql"
        DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql
    fi
    systemctl enable --now postgresql.service >/dev/null
    # Create role (peer auth via Unix socket, no password) and database.
    if sudo -u postgres psql -tAc \
            "SELECT 1 FROM pg_roles WHERE rolname='${OPP_CI_USER}'" \
            2>/dev/null | grep -q 1; then
        echo "    role '${OPP_CI_USER}' already exists"
    else
        sudo -u postgres createuser "${OPP_CI_USER}"
        echo "    created role '${OPP_CI_USER}'"
    fi
    if sudo -u postgres psql -tAc \
            "SELECT 1 FROM pg_database WHERE datname='${OPP_CI_DB}'" \
            2>/dev/null | grep -q 1; then
        echo "    database '${OPP_CI_DB}' already exists"
    else
        sudo -u postgres createdb -O "${OPP_CI_USER}" "${OPP_CI_DB}"
        echo "    created database '${OPP_CI_DB}' owned by '${OPP_CI_USER}'"
    fi
    # Postgres 15+ revokes CREATE on schema public from PUBLIC, so the
    # database owner alone can't add tables there. Grant it explicitly.
    sudo -u postgres psql -d "${OPP_CI_DB}" -c \
        "GRANT ALL ON SCHEMA public TO \"${OPP_CI_USER}\"" >/dev/null
    echo "    granted ALL on schema public to '${OPP_CI_USER}'"
    # Ask the running cluster which port it's on, rather than assuming 5432.
    PG_PORT="$(sudo -u postgres psql -tAc 'SHOW port' 2>/dev/null | tr -d ' ')"
    if [[ -z "$PG_PORT" ]]; then
        PG_PORT=5432
    fi
    POSTGRES_SOCKET_URL="postgresql:///${OPP_CI_DB}?host=/var/run/postgresql&port=${PG_PORT}"
    echo "    cluster on port ${PG_PORT}"
fi

echo "==> Installing unit files into $SYSTEMD_DIR"
install -m 0644 "$SCRIPT_DIR/opp_ci.target"                       "$SYSTEMD_DIR/opp_ci.target"
install -m 0644 "$SCRIPT_DIR/opp_ci-serve.service"                "$SYSTEMD_DIR/opp_ci-serve.service"
install -m 0644 "$SCRIPT_DIR/opp_ci-worker@.service"              "$SYSTEMD_DIR/opp_ci-worker@.service"
install -m 0644 "$SCRIPT_DIR/opp_ci-serve-cert.path"              "$SYSTEMD_DIR/opp_ci-serve-cert.path"
install -m 0644 "$SCRIPT_DIR/opp_ci-serve-cert-reload.service"    "$SYSTEMD_DIR/opp_ci-serve-cert-reload.service"

# TLS drop-in shipped as .example so the installer doesn't silently turn
# on CAP_NET_BIND_SERVICE. Operator renames it to tls.conf to activate.
install -d -m 0755 "$SERVE_DROPIN_DIR"
if [[ -e "$SERVE_DROPIN_DIR/tls.conf" ]]; then
    echo "    keeping existing $SERVE_DROPIN_DIR/tls.conf"
elif [[ -e "$SERVE_DROPIN_DIR/tls.conf.example" ]]; then
    echo "    keeping existing $SERVE_DROPIN_DIR/tls.conf.example"
else
    install -m 0644 "$SCRIPT_DIR/dropins/tls.conf.example" \
                    "$SERVE_DROPIN_DIR/tls.conf.example"
    echo "    wrote $SERVE_DROPIN_DIR/tls.conf.example (inactive — rename to tls.conf to enable)"
fi

# Cloudflare Origin CA bundle — for workers that bypass Cloudflare's
# edge and connect direct to the origin presenting an Origin Certificate.
install -m 0644 -o root -g "$OPP_CI_GROUP" \
    "$SCRIPT_DIR/cloudflare-origin-ca.pem" \
    "$TLS_DIR/cloudflare-origin-ca.pem"

echo "==> Seeding $CONFIG_DIR with example env files (only if missing)"
install_example() {
    local src="$1" dst="$2" owner="$3" mode="$4"
    if [[ -e "$dst" ]]; then
        echo "    keeping existing $dst"
    else
        install -o "$owner" -g "$OPP_CI_GROUP" -m "$mode" "$src" "$dst"
        echo "    wrote $dst"
    fi
}
install_example "$SCRIPT_DIR/opp_ci.env.example"  "$CONFIG_DIR/opp_ci.env"      root        0640
install_example "$SCRIPT_DIR/serve.env.example"   "$CONFIG_DIR/serve.env"       root        0640
install_example "$SCRIPT_DIR/worker.env.example"  "$WORKER_CONFIG_DIR/default.env" \
                                                                 "$OPP_CI_USER" 0600

if [[ "$WITH_POSTGRES" -eq 1 ]]; then
    # If OPP_CI_DATABASE_URL is missing (commented or absent), append the
    # local-socket URL. Don't touch existing active settings — the user
    # may have pointed at a remote DB on purpose.
    if grep -Eq '^OPP_CI_DATABASE_URL=' "$CONFIG_DIR/opp_ci.env"; then
        echo "    OPP_CI_DATABASE_URL already set, not changing"
    else
        printf '\nOPP_CI_DATABASE_URL=%s\n' "$POSTGRES_SOCKET_URL" \
            >> "$CONFIG_DIR/opp_ci.env"
        echo "    appended OPP_CI_DATABASE_URL=$POSTGRES_SOCKET_URL"
    fi
fi

echo "==> Reloading systemd"
systemctl daemon-reload

cat <<EOF

opp_ci systemd units installed.

Next steps:
  1. Edit /etc/opp_ci/opp_ci.env (database URL, project paths).
  2. For a coordinator host:
       edit /etc/opp_ci/serve.env, then
       sudo systemctl enable --now opp_ci-serve.service
  3. For a worker host:
       register the worker on the coordinator:
         opp_ci worker register --name <name> [--auto-tags]
       paste the token into /etc/opp_ci/workers/<name>.env, then
         sudo systemctl enable --now opp_ci-worker@<name>.service
  4. To enable the whole stack on boot:
       sudo systemctl enable opp_ci.target

  5. (Optional) Enable HTTPS in serve itself:
       Drop fullchain.pem + privkey.pem into /etc/opp_ci/tls/
       (use \`opp_ci tls-selfsign\` for a lab cert, or paste your
       Cloudflare Origin Certificate). Uncomment the TLS block in
       /etc/opp_ci/serve.env. For port 443 also:
         sudo mv /etc/systemd/system/opp_ci-serve.service.d/tls.conf.example \\
                 /etc/systemd/system/opp_ci-serve.service.d/tls.conf
         sudo systemctl daemon-reload
         sudo systemctl enable --now opp_ci-serve-cert.path
       See doc/ssl.md for the full recipe.

Logs:    journalctl -fu opp_ci-serve
         journalctl -fu opp_ci-worker@default
Status:  systemctl status opp_ci.target
EOF
