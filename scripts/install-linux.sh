#!/bin/sh
# Install from a trusted checkout. Re-running preserves data and environment overrides.
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo sh scripts/install-linux.sh /opt/hwmonitor" >&2
    exit 1
fi
app_dir=$(realpath "${1:-$(dirname "$0")/..}")
case "$app_dir" in
    *[!a-zA-Z0-9_./-]*) echo "Application path must contain only letters, digits, / . _ -" >&2; exit 1 ;;
esac
case "$app_dir" in
    /home/*|/root|/root/*|/run/user/*) echo "The service cannot read $app_dir (ProtectHome); copy the project to /opt/hwmonitor first" >&2; exit 1 ;;
esac
test -f "$app_dir/pyproject.toml"
python_bin=${PYTHON:-python3}
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 11), "Python >=3.11 required"'
if ! id hwmonitor >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/hwmonitor --shell /usr/sbin/nologin hwmonitor
fi
install -d -m 0700 -o hwmonitor -g hwmonitor /var/lib/hwmonitor
# Stop SQLite before backing up the database and its WAL. No live-file copies.
was_active=false
restart_previous() {
    if [ "$was_active" = true ]; then systemctl start hwmonitor || true; fi
}
# Installed before stopping: a failed backup must not leave the old service down.
trap restart_previous EXIT
if systemctl is-active --quiet hwmonitor; then
    was_active=true
    systemctl stop hwmonitor
fi
if [ -f /var/lib/hwmonitor/settings.json ] || [ -f /var/lib/hwmonitor/history.sqlite3 ]; then
    install -d -m 0700 /var/backups/hwmonitor
    backup=$(mktemp -d /var/backups/hwmonitor/upgrade-XXXXXXXX)
    cp -a /var/lib/hwmonitor/. "$backup/"
    echo "Data backup: $backup"
fi
if [ ! -x "$app_dir/.venv/bin/python" ]; then
    "$python_bin" -m venv "$app_dir/.venv"
fi
"$app_dir/.venv/bin/python" -m pip install "$app_dir"
sed "s|@APP_DIR@|$app_dir|g" "$app_dir/deploy/systemd/hwmonitor.service" > /etc/systemd/system/hwmonitor.service
chmod 0644 /etc/systemd/system/hwmonitor.service
systemctl daemon-reload
systemctl enable --now hwmonitor
trap - EXIT
echo "Installed. Check: systemctl status hwmonitor; journalctl -u hwmonitor -n 50"
