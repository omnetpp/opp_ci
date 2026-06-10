#!/bin/bash
# Removes the opp_ci worker LaunchDaemons (macOS), leaving config
# (/etc/opp_ci), state (/usr/local/var/opp_ci), logs, and the opp_ci
# service account in place so a re-install doesn't lose tokens or caches.
# Same preservation philosophy as packaging/systemd/uninstall.sh.
#
# Run as root:    sudo packaging/launchd/uninstall.sh

set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "uninstall.sh (launchd) is macOS-only." >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "uninstall.sh must be run as root (try: sudo $0)" >&2
    exit 1
fi

DAEMON_DIR="/Library/LaunchDaemons"
LABEL_PREFIX="org.omnetpp.opp_ci.worker"
INSTALL_DIR="/opt/opp_ci"
NEWSYSLOG_DIR="/etc/newsyslog.d"

echo "==> Booting out and removing worker daemons"
shopt -s nullglob
found=0
for plist in "$DAEMON_DIR/$LABEL_PREFIX."*.plist; do
    found=1
    label="$(basename "$plist" .plist)"
    echo "    bootout + remove $label"
    # Ignore errors — the job may already be unloaded.
    launchctl bootout "system/$label" 2>/dev/null || true
    rm -f "$plist"
done
if [[ "$found" -eq 0 ]]; then
    echo "    no worker daemons found in $DAEMON_DIR"
fi

echo "==> Removing newsyslog drop-in"
rm -f "$NEWSYSLOG_DIR/opp_ci.conf"

cat <<EOF

opp_ci worker LaunchDaemons removed.

Preserved (delete manually if you also want them gone):
  $INSTALL_DIR              (source + venv + bin/ wrapper)
  /opt/opp_env              (sibling repo, if installed)
  /opt/opp_repl             (sibling repo, if installed)
  /etc/opp_ci/              (config, including worker tokens)
  /usr/local/var/opp_ci/    (state, caches, ~opp_ci)
  /usr/local/var/log/opp_ci/ (worker logs)
  account 'opp_ci'          (remove with:
                               sudo dscl . -delete /Users/opp_ci
                               sudo dscl . -delete /Groups/opp_ci)
EOF
