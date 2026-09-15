# Dedicated single-host native `static_runner`

This bundle deploys the smallest supported remote programming-worker topology for MindRoom **2026.9.114**:

```text
primary MindRoom (coding + shell proxied; github remains local)
  -> loopback:18766
  -> restricted SSH local-forward
  -> dedicated runner host loopback:8766
  -> one native MindRoom static_runner container
```

No custom `repo_workspace`, plugin, Kubernetes cluster, Docker socket, or runtime patch is involved. The runner uses MindRoom worker protocol **1** and the native `/api/sandbox-runner` API.

## Security properties

- The bootstrap resolves `ghcr.io/mindroom-ai/mindroom:2026.9.114` to an immutable digest; systemd starts that digest with `--pull=never`.
- The runner listens through host networking, but nftables rejects port 8766 on every non-loopback path. The primary reaches it only through SSH forwarding.
- nftables rejects every **new** packet owned by workload UID 61184. Only replies on established inbound runner requests are allowed. This is infrastructure-enforced deny-all workload egress, independent of command/environment settings.
- The container runs as UID/GID 61184, drops every Linux capability, enables `no-new-privileges`, uses Podman's default seccomp policy, has a read-only root, and receives no control-plane socket.
- `/app/workspace` (2 GiB) and `/tmp` (256 MiB) are tmpfs. They disappear whenever the runner restarts. No primary data, source, configuration, or credential store is mounted.
- The only injected secret is the dedicated runner bearer token. No `.env`, GitHub, Matrix, model-provider, database, or primary-runtime credential is supplied. Rotate the runner token by replacing both endpoint copies and restarting the runner and primary.
- Limits: 2 CPUs, 4 GiB RAM with no extra swap, 256 PIDs, 1,024 file descriptors, 120-second native per-request subprocess timeout, and a one-hour container lifetime ceiling (systemd then restarts it, killing background jobs). MindRoom 2026.9.114 additionally bounds shell capture to 50 KiB per stream / 10,000 lines and allows at most 16 background shell processes.
- The primary configuration below explicitly selects `static_runner`, selects only `coding,shell`, and keeps `MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS=false`; missing proxy URL/token therefore fails closed instead of executing those tools locally.

The runner auth token is intentionally available to the runner process; it is not an upstream credential. Because untrusted code shares the runner's PID namespace in this smallest topology, assume it may learn that same-runner token. Loopback-only ingress, SSH authorization, and host firewalling prevent that token from reaching another runner or the primary.

## Prerequisites

Use one dedicated Linux VM/host with:

- systemd, nftables, rootful Podman, curl, OpenSSH server, and `sha256sum`;
- no unrelated host account assigned UID 61184;
- inbound SSH allowed only from the primary host at the cloud firewall/security-group layer;
- no primary storage/configuration mounted on the host.

The bundle does not install packages or alter global firewall policy. Existing host SSH hardening remains the operator's responsibility.

## 1. Bootstrap the runner host

Create the token file outside the repository without printing the token:

```bash
sudo install -d -m 0700 /root/mindroom-runner-bootstrap
sudo sh -c 'umask 077; printf "MINDROOM_SANDBOX_PROXY_TOKEN=%s\\n" "$(openssl rand -hex 32)" > /root/mindroom-runner-bootstrap/runner.env'
sudo ./bootstrap.sh /root/mindroom-runner-bootstrap/runner.env
```

`bootstrap.sh` validates prerequisites and token shape, pulls the versioned image before policy activation, pins its digest in `/etc/mindroom-static-runner/deployment.env`, installs two systemd units, starts the service, and runs validation. It never prints the token.

## 2. Authorize one revocable primary-host tunnel

On the primary host, generate a dedicated key and record the runner host key:

```bash
sudo install -d -m 0700 /etc/mindroom
sudo ssh-keygen -q -t ed25519 -N '' -f /etc/mindroom/runner-tunnel.key
sudo chmod 0600 /etc/mindroom/runner-tunnel.key
ssh-keyscan -H RUNNER_HOST | sudo tee -a /etc/ssh/ssh_known_hosts >/dev/null
```

On the runner host, create a dedicated account and install only that public key. Replace `PASTE_PRIMARY_PUBLIC_KEY` with the single `.pub` line; it is not a secret:

```bash
sudo useradd --create-home --shell /bin/false mindroom-tunnel
sudo install -d -o mindroom-tunnel -g mindroom-tunnel -m 0700 /home/mindroom-tunnel/.ssh
printf '%s\n' 'restrict,port-forwarding,permitopen="127.0.0.1:8766" PASTE_PRIMARY_PUBLIC_KEY' | \
  sudo tee /home/mindroom-tunnel/.ssh/authorized_keys >/dev/null
sudo chown mindroom-tunnel:mindroom-tunnel /home/mindroom-tunnel/.ssh/authorized_keys
sudo chmod 0600 /home/mindroom-tunnel/.ssh/authorized_keys
```

Copy `mindroom-static-runner-tunnel.service.example` to the primary host, replace only `RUNNER_HOST` and (if needed) `User=mindroom`, install it as `/etc/systemd/system/mindroom-static-runner-tunnel.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mindroom-static-runner-tunnel.service
curl --fail --silent http://127.0.0.1:18766/healthz
```

Revocation is immediate: remove that `authorized_keys` line and terminate its SSH session, or stop the tunnel unit. The runner HTTP port is not remotely reachable without the SSH authorization.

## 3. Configure the primary MindRoom runtime (no local fallback)

Copy the same generated token securely into the primary's existing secret manager/environment; do **not** commit it or add it to `config.yaml`:

```dotenv
MINDROOM_WORKER_BACKEND=static_runner
MINDROOM_SANDBOX_PROXY_URL=http://127.0.0.1:18766
MINDROOM_SANDBOX_PROXY_TOKEN=<same-dedicated-64-hex-token>
MINDROOM_SANDBOX_EXECUTION_MODE=selective
MINDROOM_SANDBOX_PROXY_TOOLS=coding,shell
MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS=false
MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS=120
MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON={}
```

Keep `github` out of `MINDROOM_SANDBOX_PROXY_TOOLS` and any agent `worker_tools`; it remains primary-local. If authored agent configuration has a `worker_tools` override, it must be exactly:

```yaml
worker_tools:
  - coding
  - shell
```

Do not add `file`, `python`, or `github` unless separately approved. Restart the primary runtime after applying its environment through the normal deployment mechanism.

## 4. Validate

Runner host:

```bash
sudo /etc/mindroom-static-runner/validate.sh
sudo systemctl --no-pager --full status mindroom-static-runner.service
```

Primary host:

```bash
curl --fail --silent http://127.0.0.1:18766/healthz
```

Expected health fields are `status=ok`, `mindroom_version=2026.9.114`, and `worker_protocol=1`. Then run controlled agent smoke calls:

1. `coding.ls` succeeds and sees only ephemeral worker scratch, not primary files.
2. `shell.run_shell_command(["sh", "-c", "printf ok"])` returns `ok` remotely.
3. `shell.run_shell_command(["sh", "-c", "test ! -e /app/config.yaml && test ! -S /var/run/docker.sock && test ! -S /run/podman/podman.sock"])` succeeds.
4. `shell.run_shell_command(["sh", "-c", "env | grep -E '(GITHUB|MATRIX|OPENAI|ANTHROPIC|TOKEN|SECRET|PASSWORD)' || true"])` shows no upstream credentials. The runner's internal sandbox auth variable is filtered from tool subprocess environments by MindRoom.
5. An outbound probe such as `curl --connect-timeout 2 https://example.com` fails.
6. Stop the tunnel and verify a coding/shell call fails; it must not execute on the primary. Restore the tunnel afterward.
7. Confirm a GitHub tool call still executes through the primary-local GitHub integration.

## Operations and rotation

```bash
# Logs contain no configured secrets unless a workload prints data itself.
sudo journalctl -u mindroom-static-runner.service --since today

# Restart clears all tmpfs scratch and background processes.
sudo systemctl restart mindroom-static-runner.service

# Token rotation: replace runner.env atomically, update the primary secret,
# restart runner, then restart primary.
sudo install -m 0600 /absolute/path/to/new-runner.env /etc/mindroom-static-runner/runner.env
sudo systemctl restart mindroom-static-runner.service
```

## Teardown / rollback

First remove the primary's static-runner environment and restart it while coding/shell remain disabled or otherwise safely routed. Then:

```bash
# Primary host
sudo systemctl disable --now mindroom-static-runner-tunnel.service

# Runner host, from this bundle
sudo ./teardown.sh
```

The teardown removes only the two named units, the `inet mindroom_static_runner` nftables table, and `/etc/mindroom-static-runner` files. It deliberately retains the cached image. Delete the restricted SSH key/account separately after confirming no other authorized use.
