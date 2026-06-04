#!/usr/bin/env bash
# Idempotent installer for the opp_ci systemd units.
#
# Creates the `opp_ci` system user, installs unit files into
# /etc/systemd/system/, seeds /etc/opp_ci/ with example env files
# (without overwriting existing ones), and ensures /opt/opp_ci has a
# Python venv with opp_ci installed editably.
#
# Run as root:    sudo packaging/systemd/install.sh
#
# After install, enable the units you actually want on this host:
#   sudo systemctl enable --now opp_ci-serve.service
#   sudo systemctl enable --now opp_ci-worker@default.service
#   sudo systemctl enable opp_ci.target

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "install.sh must be run as root (try: sudo $0)" >&2
    exit 1
fi

# Resolve script directory and the repo root (two levels up).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

OPP_CI_USER="opp_ci"
OPP_CI_GROUP="opp_ci"
INSTALL_DIR="/opt/opp_ci"
STATE_DIR="/var/lib/opp_ci"
CONFIG_DIR="/etc/opp_ci"
WORKER_CONFIG_DIR="$CONFIG_DIR/workers"
SYSTEMD_DIR="/etc/systemd/system"

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
"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR"

# The venv needs to be readable & executable by the opp_ci user, but the
# rest of /opt/opp_ci can stay root-owned.
chown -R "$OPP_CI_USER:$OPP_CI_GROUP" "$INSTALL_DIR/.venv"

echo "==> Installing unit files into $SYSTEMD_DIR"
install -m 0644 "$SCRIPT_DIR/opp_ci.target"            "$SYSTEMD_DIR/opp_ci.target"
install -m 0644 "$SCRIPT_DIR/opp_ci-serve.service"     "$SYSTEMD_DIR/opp_ci-serve.service"
install -m 0644 "$SCRIPT_DIR/opp_ci-worker@.service"   "$SYSTEMD_DIR/opp_ci-worker@.service"

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

Logs:    journalctl -fu opp_ci-serve
         journalctl -fu opp_ci-worker@default
Status:  systemctl status opp_ci.target
EOF
