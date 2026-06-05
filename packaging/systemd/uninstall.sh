#!/usr/bin/env bash
# Removes the opp_ci systemd units, leaving config (/etc/opp_ci) and
# state (/var/lib/opp_ci) untouched so a re-install picks up where it
# left off. Run as root.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "uninstall.sh must be run as root (try: sudo $0)" >&2
    exit 1
fi

SYSTEMD_DIR="/etc/systemd/system"
INSTALL_DIR="/opt/opp_ci"

echo "==> Stopping and disabling units (if running)"
# Stop instances first, then base units. Ignore errors — units may not be enabled.
systemctl stop 'opp_ci-worker@*.service' 2>/dev/null || true
systemctl disable 'opp_ci-worker@*.service' 2>/dev/null || true
systemctl stop opp_ci-serve-cert.path 2>/dev/null || true
systemctl disable opp_ci-serve-cert.path 2>/dev/null || true
systemctl stop opp_ci-serve-cert-reload.service 2>/dev/null || true
systemctl stop opp_ci-serve.service 2>/dev/null || true
systemctl disable opp_ci-serve.service 2>/dev/null || true
systemctl disable opp_ci.target 2>/dev/null || true

echo "==> Removing unit files"
rm -f "$SYSTEMD_DIR/opp_ci.target"
rm -f "$SYSTEMD_DIR/opp_ci-serve.service"
rm -f "$SYSTEMD_DIR/opp_ci-worker@.service"
rm -f "$SYSTEMD_DIR/opp_ci-serve-cert.path"
rm -f "$SYSTEMD_DIR/opp_ci-serve-cert-reload.service"
# Drop-in dir: remove the shipped .example, but leave any operator-authored
# tls.conf in place so the next install isn't a TLS-status surprise.
rm -f "$SYSTEMD_DIR/opp_ci-serve.service.d/tls.conf.example"
rmdir --ignore-fail-on-non-empty "$SYSTEMD_DIR/opp_ci-serve.service.d" 2>/dev/null || true

systemctl daemon-reload

cat <<EOF

opp_ci systemd units removed.

Preserved (delete manually if you also want them gone):
  $INSTALL_DIR              (source + venv)
  /etc/opp_ci/              (config, including worker tokens and tls/)
  /var/lib/opp_ci/          (sqlite DB, caches)
  user/group 'opp_ci'       (run: sudo userdel opp_ci && sudo groupdel opp_ci)
EOF
