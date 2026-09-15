#!/usr/bin/env bash
set -euo pipefail
readonly ETC_DIR=/etc/mindroom-static-runner
source "$ETC_DIR/deployment.env"
source "$ETC_DIR/runner.env"
fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

health=$(curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8766/healthz) || fail "health endpoint unavailable"
grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"$health" || fail "health status is not ok"
grep -Eq '"mindroom_version"[[:space:]]*:[[:space:]]*"2026\.9\.114"' <<<"$health" || fail "runner version mismatch: $health"
grep -Eq '"worker_protocol"[[:space:]]*:[[:space:]]*1' <<<"$health" || fail "worker protocol mismatch: $health"
pass "health reports MindRoom 2026.9.114 protocol 1"

workers=$(curl --fail --silent --show-error --max-time 5 -H "x-mindroom-sandbox-token: $MINDROOM_SANDBOX_PROXY_TOKEN" http://127.0.0.1:8766/api/sandbox-runner/workers) || fail "authenticated runner API unavailable"
[[ $workers == \[* ]] || fail "unexpected authenticated API response"
pass "bearer-authenticated runner API responds"

inspect=$(podman inspect mindroom-static-runner)
grep -Fq '"ReadonlyRootfs": true' <<<"$inspect" || fail "container root is not read-only"
grep -Fq '"User": "61184:61184"' <<<"$inspect" || fail "container user mismatch"
! grep -Eq '/var/run/(docker|podman)\.sock|/run/(docker|podman)/' <<<"$inspect" || fail "control-plane socket is mounted"
! grep -Eq '/app/config\.yaml|mindroom_data|MATRIX_|GITHUB_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY' <<<"$inspect" || fail "forbidden mount or credential name found"
pass "container is unprivileged, read-only, and has no forbidden mounts/credential names"

nft list table inet mindroom_static_runner | grep -Fq 'meta skuid 61184 reject' || fail "workload egress reject rule missing"
nft list table inet mindroom_static_runner | grep -Fq 'tcp dport 8766 reject' || fail "external runner ingress reject rule missing"
pass "infrastructure deny-all workload egress and loopback-only runner ingress are active"

podman exec mindroom-static-runner sh -c 'test -w /app/workspace && test -w /tmp' || fail "scratch is not writable"
if podman exec mindroom-static-runner /app/.venv/bin/python -c 'import socket; socket.create_connection(("1.1.1.1", 443), 2)' 2>/dev/null; then
  fail "workload unexpectedly opened an outbound connection"
fi
pass "live workload egress probe was denied"

echo "VALIDATION COMPLETE"
