#!/usr/bin/env bash
set -euo pipefail

readonly IMAGE_TAG="ghcr.io/mindroom-ai/mindroom:2026.9.114"
readonly ETC_DIR="/etc/mindroom-static-runner"
readonly UNIT_DIR="/etc/systemd/system"
readonly HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "Usage: sudo $0 /absolute/path/to/runner.env" >&2
  exit 2
}

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ $# -eq 1 && $1 = /* && -f $1 ]] || usage
TOKEN_ENV=$1
for command in podman nft systemctl curl sha256sum; do
  command -v "$command" >/dev/null || { echo "Missing prerequisite: $command" >&2; exit 1; }
done

mode=$(stat -c '%a' "$TOKEN_ENV")
(( (8#$mode & 077) == 0 )) || { echo "$TOKEN_ENV must not be group/world accessible (use chmod 0600)." >&2; exit 1; }
# Accept only one exact secret declaration; never print its value.
[[ $(grep -Ec '^MINDROOM_SANDBOX_PROXY_TOKEN=[0-9A-Fa-f]{64}$' "$TOKEN_ENV") -eq 1 ]] || {
  echo "$TOKEN_ENV must contain one MINDROOM_SANDBOX_PROXY_TOKEN with exactly 64 hexadecimal characters." >&2
  exit 1
}
[[ $(grep -Evc '^(#.*|[[:space:]]*|MINDROOM_SANDBOX_PROXY_TOKEN=[0-9A-Fa-f]{64})$' "$TOKEN_ENV") -eq 0 ]] || {
  echo "$TOKEN_ENV contains unsupported entries." >&2
  exit 1
}
getent passwd 61184 >/dev/null && { echo "Host UID 61184 is already assigned; dedicate a clean host or review the UID before deployment." >&2; exit 1; }

# Pull before activating the workload egress policy. The service later uses the immutable digest and --pull=never.
podman pull "$IMAGE_TAG" >/dev/null
IMAGE_DIGEST=$(podman image inspect "$IMAGE_TAG" --format '{{index .RepoDigests 0}}')
[[ $IMAGE_DIGEST == ghcr.io/mindroom-ai/mindroom@sha256:* ]] || { echo "Could not resolve an immutable image digest." >&2; exit 1; }

install -d -m 0700 "$ETC_DIR"
install -m 0600 "$TOKEN_ENV" "$ETC_DIR/runner.env"
printf 'MINDROOM_IMAGE=%s\nMINDROOM_EXPECTED_VERSION=2026.9.114\nMINDROOM_EXPECTED_PROTOCOL=1\n' "$IMAGE_DIGEST" >"$ETC_DIR/deployment.env"
chmod 0600 "$ETC_DIR/deployment.env"
install -m 0600 "$HERE/mindroom-static-runner.nft" "$ETC_DIR/mindroom-static-runner.nft"
install -m 0644 "$HERE/mindroom-static-runner.service" "$UNIT_DIR/mindroom-static-runner.service"
install -m 0644 "$HERE/mindroom-static-runner-egress.service" "$UNIT_DIR/mindroom-static-runner-egress.service"
install -m 0755 "$HERE/validate.sh" "$ETC_DIR/validate.sh"

systemctl daemon-reload
systemctl enable --now mindroom-static-runner.service
"$ETC_DIR/validate.sh"
cat <<'MSG'
Runner installed. It is reachable only through host loopback.
Next, install a restricted SSH public key for the primary host and establish the tunnel described in README.md.
MSG
