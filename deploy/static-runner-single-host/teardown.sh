#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
systemctl disable --now mindroom-static-runner.service 2>/dev/null || true
systemctl disable --now mindroom-static-runner-egress.service 2>/dev/null || true
podman rm -f --ignore mindroom-static-runner 2>/dev/null || true
# Deliberately keep the pinned image cached; remove it manually only if desired.
rm -f /etc/systemd/system/mindroom-static-runner.service /etc/systemd/system/mindroom-static-runner-egress.service
rm -f /etc/mindroom-static-runner/runner.env /etc/mindroom-static-runner/deployment.env /etc/mindroom-static-runner/mindroom-static-runner.nft /etc/mindroom-static-runner/validate.sh
rmdir /etc/mindroom-static-runner 2>/dev/null || true
systemctl daemon-reload
echo "MindRoom static runner service, policy, and local secret copy removed."
