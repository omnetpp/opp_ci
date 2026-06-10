#!/bin/bash
# Idempotent installer for the opp_ci worker as a launchd LaunchDaemon (macOS).
#
# This is the macOS analogue of packaging/systemd/install.sh, but scoped to
# WORKERS ONLY — no serve, no coordinator, no PostgreSQL, no TLS path units.
# A macOS worker polls a remote coordinator and needs only outbound network
# access. See doc/launchd.md.
#
# It:
#   - creates the hidden `opp_ci` service account (group + user, UID < 500),
#   - creates /opt/opp_ci, /etc/opp_ci/workers, /usr/local/var/opp_ci,
#     /usr/local/var/log/opp_ci with the right owners/modes,
#   - syncs the source tree to /opt/opp_ci and builds a venv
#     (pip install -e .[client,podman]); syncs sibling opp_env/opp_repl,
#   - installs the env-sourcing wrapper and the opp_ci-workers helper,
#   - seeds /etc/opp_ci/opp_ci.env and workers/default.env (only if missing),
#   - renders a per-worker plist into /Library/LaunchDaemons/ for each name,
#   - installs a newsyslog drop-in for log rotation.
#
# It does NOT bootstrap (start) any daemon — that is the next step, after you
# paste the worker token into its env file.
#
# Run as root:    sudo packaging/launchd/install.sh [worker-name ...]
#   sudo packaging/launchd/install.sh                  # → worker "default"
#   sudo packaging/launchd/install.sh builder-1 nix-1  # → two workers

set -euo pipefail

WORKER_NAMES=()
for arg in "$@"; do
    case "$arg" in
        -h|--help)
            sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        -*)
            echo "Unknown argument: $arg" >&2
            exit 2
            ;;
        *)
            WORKER_NAMES+=("$arg")
            ;;
    esac
done
if [[ ${#WORKER_NAMES[@]} -eq 0 ]]; then
    WORKER_NAMES=(default)
fi

if [[ "$(uname)" != "Darwin" ]]; then
    echo "install.sh (launchd) is macOS-only. On Linux use packaging/systemd/install.sh." >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "install.sh must be run as root (try: sudo $0)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

OPP_CI_USER="opp_ci"
OPP_CI_GROUP="opp_ci"
INSTALL_DIR="/opt/opp_ci"
CONFIG_DIR="/etc/opp_ci"
WORKER_CONFIG_DIR="$CONFIG_DIR/workers"
STATE_DIR="/usr/local/var/opp_ci"
LOG_DIR="/usr/local/var/log/opp_ci"
DAEMON_DIR="/Library/LaunchDaemons"
LABEL_PREFIX="org.omnetpp.opp_ci.worker"
NEWSYSLOG_DIR="/etc/newsyslog.d"

# ---------------------------------------------------------------------------
echo "==> Creating service account '$OPP_CI_USER' (if missing)"
# macOS has no useradd. Create a hidden role account with dscl. macOS hides
# UID<500 accounts from the login screen anyway; we also set IsHidden=1.
# Pick the same free UID for the group (GID) and user (UID) so they pair up.

dscl_user_exists() { dscl . -read "/Users/$OPP_CI_USER" >/dev/null 2>&1; }
dscl_group_exists() { dscl . -read "/Groups/$OPP_CI_GROUP" >/dev/null 2>&1; }

pick_free_id() {
    # Lowest free id in [200,499] not used by any user OR group.
    local used id
    used="$(
        { dscl . -list /Users UniqueID 2>/dev/null
          dscl . -list /Groups PrimaryGroupID 2>/dev/null; } \
        | awk '{print $NF}'
    )"
    for id in $(seq 200 499); do
        if ! printf '%s\n' "$used" | grep -qx "$id"; then
            echo "$id"
            return 0
        fi
    done
    echo "no free UID/GID in 200-499" >&2
    return 1
}

if dscl_user_exists; then
    OPP_CI_ID="$(dscl . -read "/Users/$OPP_CI_USER" UniqueID 2>/dev/null | awk '{print $2}')"
    echo "    user '$OPP_CI_USER' already exists (UID $OPP_CI_ID)"
else
    OPP_CI_ID="$(pick_free_id)"
    echo "    allocating UID/GID $OPP_CI_ID"
    if ! dscl_group_exists; then
        dscl . -create "/Groups/$OPP_CI_GROUP"
        dscl . -create "/Groups/$OPP_CI_GROUP" PrimaryGroupID "$OPP_CI_ID"
        dscl . -create "/Groups/$OPP_CI_GROUP" RealName "opp_ci CI worker"
    fi
    dscl . -create "/Users/$OPP_CI_USER"
    dscl . -create "/Users/$OPP_CI_USER" UniqueID "$OPP_CI_ID"
    dscl . -create "/Users/$OPP_CI_USER" PrimaryGroupID "$OPP_CI_ID"
    dscl . -create "/Users/$OPP_CI_USER" NFSHomeDirectory "$STATE_DIR"
    dscl . -create "/Users/$OPP_CI_USER" UserShell /bin/bash
    dscl . -create "/Users/$OPP_CI_USER" RealName "opp_ci CI worker"
    dscl . -create "/Users/$OPP_CI_USER" IsHidden 1
    # No password / no interactive login (role account).
    dscl . -create "/Users/$OPP_CI_USER" Password '*'
fi
# Ensure the group exists even if the user pre-existed without it.
if ! dscl_group_exists; then
    dscl . -create "/Groups/$OPP_CI_GROUP"
    dscl . -create "/Groups/$OPP_CI_GROUP" PrimaryGroupID "$OPP_CI_ID"
    dscl . -create "/Groups/$OPP_CI_GROUP" RealName "opp_ci CI worker"
fi

# ---------------------------------------------------------------------------
echo "==> Creating directories"
install -d -o root         -g wheel          -m 0755 "$INSTALL_DIR"
install -d -o root         -g "$OPP_CI_GROUP" -m 0750 "$CONFIG_DIR"
install -d -o "$OPP_CI_USER" -g "$OPP_CI_GROUP" -m 0750 "$WORKER_CONFIG_DIR"
install -d -o "$OPP_CI_USER" -g "$OPP_CI_GROUP" -m 0750 "$STATE_DIR"
install -d -o "$OPP_CI_USER" -g "$OPP_CI_GROUP" -m 0750 "$LOG_DIR"

# ---------------------------------------------------------------------------
sync_sibling_repo() {
    # Sync a sibling git checkout (or clone it) into /opt/<name>, mirroring
    # the dev layout where opp_ci, opp_env, opp_repl live side by side.
    local name="$1" clone_url="$2"
    local src="$REPO_ROOT/../$name" dest="/opt/$name"
    if [[ -d "$src/.git" ]]; then
        echo "    syncing $src → $dest"
        if command -v rsync >/dev/null 2>&1; then
            rsync -a --delete \
                --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' \
                "$src/" "$dest/"
        else
            mkdir -p "$dest"
            cp -a "$src/." "$dest/"
        fi
    elif [[ -d "$dest/.git" ]]; then
        echo "    keeping existing $dest (no sibling source — \`cd $dest && git pull\` to upgrade)"
    else
        echo "    cloning $clone_url → $dest"
        git clone --depth 1 "$clone_url" "$dest"
    fi
    chown -R root:wheel "$dest"
}

echo "==> Syncing source tree to $INSTALL_DIR"
# Keep .git/ so setuptools-scm can derive the version. Exclude the venv
# (rebuilt below), caches, and any local SQLite DB from a dev checkout.
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
chown -R root:wheel "$INSTALL_DIR"

echo "==> Syncing sibling repos (opp_env, opp_repl)"
sync_sibling_repo opp_env  https://github.com/omnetpp/opp_env.git
sync_sibling_repo opp_repl https://github.com/omnetpp/opp_repl.git

echo "==> Creating Python venv and installing opp_ci (editable)"
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
# Worker-only extras: client (requests for --remote) + podman (yaml/jinja2
# for podman-isolation jobs). No web/postgres — this host never runs serve.
EXTRAS="client,podman"
# Install siblings editable first so opp_ci's dependency on opp_repl resolves
# to the local checkout, not PyPI. opp_repl carries [all] so worker-driven
# REPL features all resolve. Mirrors packaging/systemd/install.sh.
declare -A SIBLING_EXTRAS=(
    [/opt/opp_env]=""
    [/opt/opp_repl]="all"
)
for sib in /opt/opp_env /opt/opp_repl; do
    if [[ -f "$sib/pyproject.toml" ]]; then
        sib_extras="${SIBLING_EXTRAS[$sib]}"
        if [[ -n "$sib_extras" ]]; then
            "$INSTALL_DIR/.venv/bin/pip" install -e "$sib[$sib_extras]"
        else
            "$INSTALL_DIR/.venv/bin/pip" install -e "$sib"
        fi
    fi
done
"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR[$EXTRAS]"
# The venv must be readable/executable by the opp_ci user; the rest of
# /opt/opp_ci can stay root-owned.
chown -R "$OPP_CI_USER:$OPP_CI_GROUP" "$INSTALL_DIR/.venv"

# ---------------------------------------------------------------------------
echo "==> Installing wrapper and helper scripts into $INSTALL_DIR/bin"
install -d -o root -g wheel -m 0755 "$INSTALL_DIR/bin"
install -o root -g wheel -m 0755 "$SCRIPT_DIR/opp_ci-worker-run" "$INSTALL_DIR/bin/opp_ci-worker-run"
install -o root -g wheel -m 0755 "$SCRIPT_DIR/opp_ci-workers"    "$INSTALL_DIR/bin/opp_ci-workers"

# ---------------------------------------------------------------------------
echo "==> Configuring opp_ci user's shell environment"
# Drop a .profile so `sudo -u opp_ci -i` and login shells activate the venv.
# launchd doesn't read this — the plist sets PATH via EnvironmentVariables.
PROFILE="$STATE_DIR/.profile"
if [[ ! -e "$PROFILE" ]]; then
    cat > "$PROFILE" <<'EOF'
# Auto-installed by packaging/launchd/install.sh.
# Activates the opp_ci venv and puts its bin/ on PATH.
if [ -f /opt/opp_ci/setenv ]; then
    . /opt/opp_ci/setenv >/dev/null
fi
export PATH="/opt/opp_ci/bin:/opt/opp_ci/.venv/bin:$PATH"
EOF
    chown "$OPP_CI_USER:$OPP_CI_GROUP" "$PROFILE"
    chmod 0644 "$PROFILE"
    echo "    wrote $PROFILE"
else
    echo "    keeping existing $PROFILE"
fi

# ---------------------------------------------------------------------------
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
# These .example files are symlinks into packaging/systemd/ so the two
# platforms can never drift; `install` follows the link and copies content.
install_example "$SCRIPT_DIR/opp_ci.env.example" "$CONFIG_DIR/opp_ci.env"          root         0640
install_example "$SCRIPT_DIR/worker.env.example" "$WORKER_CONFIG_DIR/default.env"  "$OPP_CI_USER" 0600

# ---------------------------------------------------------------------------
echo "==> Rendering worker plists into $DAEMON_DIR"
for name in "${WORKER_NAMES[@]}"; do
    plist="$DAEMON_DIR/$LABEL_PREFIX.$name.plist"
    if [[ -e "$plist" ]]; then
        echo "    keeping existing $plist"
    else
        sed "s/__NAME__/$name/g" "$SCRIPT_DIR/worker.plist.template" > "$plist"
        echo "    wrote $plist"
    fi
    # launchd refuses to load a plist unless it is owned by root and not
    # group/world-writable — a common gotcha. Enforce it every run.
    chown root:wheel "$plist"
    chmod 0644 "$plist"
    # Seed a per-worker env file (only if missing) so the operator has a
    # 0600 token file to edit before bootstrapping.
    wenv="$WORKER_CONFIG_DIR/$name.env"
    if [[ "$name" != "default" ]]; then
        install_example "$SCRIPT_DIR/worker.env.example" "$wenv" "$OPP_CI_USER" 0600
    fi
done

# ---------------------------------------------------------------------------
echo "==> Installing newsyslog drop-in for log rotation"
install -d -m 0755 "$NEWSYSLOG_DIR"
install -o root -g wheel -m 0644 "$SCRIPT_DIR/newsyslog-opp_ci.conf" "$NEWSYSLOG_DIR/opp_ci.conf"

# ---------------------------------------------------------------------------
cat <<EOF

opp_ci worker LaunchDaemon(s) installed: ${WORKER_NAMES[*]}

Next steps (per worker — example for "${WORKER_NAMES[0]}"):
  1. Register the worker on the (remote) coordinator and copy the token.
     --auto-tags reads /etc/os-release (Linux-only), so on macOS pass
     explicit tags instead:
       opp_ci worker register --remote https://ci.example.org \\
           --name ${WORKER_NAMES[0]} \\
           --tags os:macos,os:macos-15,arch:arm64
     (Omit a 'podman' tag — podman-on-macOS runs in a per-user VM and is
      out of scope for headless workers; see doc/launchd.md.)

  2. Paste the token into the per-worker env file (0600 opp_ci:opp_ci):
       sudo vi   $WORKER_CONFIG_DIR/${WORKER_NAMES[0]}.env
       sudo chown $OPP_CI_USER:$OPP_CI_GROUP $WORKER_CONFIG_DIR/${WORKER_NAMES[0]}.env
       sudo chmod 600 $WORKER_CONFIG_DIR/${WORKER_NAMES[0]}.env

  3. Bootstrap (load + start; RunAtLoad starts it immediately):
       sudo launchctl bootstrap system $DAEMON_DIR/$LABEL_PREFIX.${WORKER_NAMES[0]}.plist

Day-to-day:
  Restart:  sudo launchctl kickstart -k system/$LABEL_PREFIX.<name>
  Stop:     sudo launchctl bootout   system/$LABEL_PREFIX.<name>
  Status:   launchctl print          system/$LABEL_PREFIX.<name>
  All:      sudo $INSTALL_DIR/bin/opp_ci-workers {start|stop|restart|status}
  Logs:     tail -f $LOG_DIR/worker-<name>.log

See doc/launchd.md for the full guide.
EOF
