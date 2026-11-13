#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo" >&2
    exit 1
fi
systemctl disable --now hwmonitor 2>/dev/null || true
rm -f /etc/systemd/system/hwmonitor.service
systemctl daemon-reload
echo "Service removed. Data, keys, backups, service user and application are preserved."
