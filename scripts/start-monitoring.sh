#!/usr/bin/env bash
set -Eeuo pipefail

# Start the remote bridge, SSH metric tunnels, Prometheus rules and dashboards.
# Override REMOTE_* variables when the rental host or remote paths change.

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}/agentrix-monitoring"
SSH_SOCKET="${RUNTIME_DIR}/ssh-control.sock"
SSH_TARGET="${REMOTE_SSH_TARGET:-root+vm-qEm2ZWBhbpAjJH7u@36.141.21.142}"
SSH_PORT="${REMOTE_SSH_PORT:-32222}"
REMOTE_DIR="${REMOTE_OBSERVABILITY_DIR:-/data/Agentrix/observability}"
REMOTE_PYTHON="${REMOTE_PYTHON:-/data/Agentrix/.venv/bin/python}"
REMOTE_BRIDGE_PORT="${REMOTE_BRIDGE_PORT:-8002}"
REMOTE_AGENT_CONTROL_PORT="${REMOTE_AGENT_CONTROL_PORT:-8003}"
REMOTE_RECOVERY_PORT="${REMOTE_RECOVERY_PORT:-8004}"
REMOTE_RECOVERY_METRICS_PORT="${REMOTE_RECOVERY_METRICS_PORT:-9004}"
REMOTE_RECOVERY_TOKEN="${REMOTE_RECOVERY_TOKEN:-}"
REMOTE_RECOVERY_USE_SYSTEMD="${REMOTE_RECOVERY_USE_SYSTEMD:-1}"
REMOTE_RECOVERY_SERVICE="${REMOTE_RECOVERY_SERVICE:-remote-agent.service}"
REMOTE_BRIDGE_UNIT="${REMOTE_BRIDGE_UNIT:-agentrix-vllm-bridge.service}"
REMOTE_VLLM_UNIT="${REMOTE_VLLM_UNIT:-vllm.service}"
REMOTE_CHECKPOINT_SOURCE="${REMOTE_CHECKPOINT_SOURCE:-/data/Agentrix/checkpoints/latest.pt}"
REMOTE_CHECKPOINT_TARGET="${REMOTE_CHECKPOINT_TARGET:-/data/Agentrix/checkpoints/active.pt}"
REMOTE_MODEL="${REMOTE_MODEL:-Qwen3.5-9B}"
LOCAL_BIND="${LOCAL_METRICS_BIND:-192.168.1.50}"

SSH_COMMON=(
  -p "$SSH_PORT"
  -o UpdateHostKeys=no
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
)

log() { printf '[monitoring] %s\n' "$*"; }
die() { printf '[monitoring] ERROR: %s\n' "$*" >&2; exit 1; }

command -v ssh >/dev/null || die "ssh is required"
command -v kubectl >/dev/null || die "kubectl is required"
command -v curl >/dev/null || die "curl is required"
[[ -n "$REMOTE_RECOVERY_TOKEN" ]] || die "REMOTE_RECOVERY_TOKEN is required for the restricted recovery agent"
mkdir -p "$RUNTIME_DIR"

control_alive() {
  ssh -S "$SSH_SOCKET" -O check "${SSH_COMMON[@]}" "$SSH_TARGET" \
    >/dev/null 2>&1
}

start_control_master() {
  if control_alive; then
    return
  fi
  rm -f "$SSH_SOCKET"
  log "connecting to remote NPU host"
  ssh -M -S "$SSH_SOCKET" -fnNT "${SSH_COMMON[@]}" "$SSH_TARGET"
}

start_remote_bridge() {
  local quoted_dir quoted_python quoted_model
  printf -v quoted_dir '%q' "$REMOTE_DIR"
  printf -v quoted_python '%q' "$REMOTE_PYTHON"
  printf -v quoted_model '%q' "$REMOTE_MODEL"
  log "starting or reusing remote Agent bridge on :${REMOTE_BRIDGE_PORT}"
  ssh -S "$SSH_SOCKET" "${SSH_COMMON[@]}" "$SSH_TARGET" \
    "cd ${quoted_dir} && if ! curl -fsS --max-time 2 http://127.0.0.1:${REMOTE_BRIDGE_PORT}/metrics >/dev/null 2>&1 || ! curl -fsS --max-time 2 http://127.0.0.1:${REMOTE_AGENT_CONTROL_PORT}/healthz >/dev/null 2>&1; then pids=\$(pgrep -f '[v]llm_agent_bridge.py --port ${REMOTE_BRIDGE_PORT}' || true); if [ -n \"\$pids\" ]; then kill \$pids || true; sleep 1; fi; nohup ${quoted_python} vllm_agent_bridge.py --port ${REMOTE_BRIDGE_PORT} --control-port ${REMOTE_AGENT_CONTROL_PORT} --router-url http://127.0.0.1:8000/routing-stats --vllm-url http://127.0.0.1:8001/metrics --npu-url http://127.0.0.1:8082/metrics --model ${quoted_model} > /tmp/vllm-agent-bridge-${REMOTE_BRIDGE_PORT}.log 2>&1 < /dev/null & fi"
}

sync_remote_recovery_agent() {
  log "syncing restricted recovery agent"
  scp -q -P "$SSH_PORT" -o UpdateHostKeys=no -o ControlPath="$SSH_SOCKET" \
    "$ROOT_DIR/recovery/remote_agent.py" "$SSH_TARGET:${REMOTE_DIR}/remote_agent.py"
}

start_remote_recovery_agent() {
  if [[ "$REMOTE_RECOVERY_USE_SYSTEMD" == "1" ]]; then
    local quoted_service
    printf -v quoted_service '%q' "$REMOTE_RECOVERY_SERVICE"
    log "starting restricted recovery agent via ${REMOTE_RECOVERY_SERVICE}"
    ssh -S "$SSH_SOCKET" "${SSH_COMMON[@]}" "$SSH_TARGET" \
      "systemctl is-enabled --quiet ${quoted_service} && systemctl restart ${quoted_service}"
    return
  fi
  local quoted_dir quoted_python quoted_token quoted_bridge quoted_vllm quoted_source quoted_target
  printf -v quoted_dir '%q' "$REMOTE_DIR"
  printf -v quoted_python '%q' "$REMOTE_PYTHON"
  printf -v quoted_token '%q' "$REMOTE_RECOVERY_TOKEN"
  printf -v quoted_bridge '%q' "$REMOTE_BRIDGE_UNIT"
  printf -v quoted_vllm '%q' "$REMOTE_VLLM_UNIT"
  printf -v quoted_source '%q' "$REMOTE_CHECKPOINT_SOURCE"
  printf -v quoted_target '%q' "$REMOTE_CHECKPOINT_TARGET"
  log "starting or reusing remote recovery agent on :${REMOTE_RECOVERY_PORT}"
  ssh -S "$SSH_SOCKET" "${SSH_COMMON[@]}" "$SSH_TARGET" \
    "cd ${quoted_dir} && if ! curl -fsS --max-time 2 http://127.0.0.1:${REMOTE_RECOVERY_PORT}/healthz >/dev/null 2>&1; then pids=\$(pgrep -f '[r]emote_agent.py --port ${REMOTE_RECOVERY_PORT}' || true); if [ -n \"\$pids\" ]; then kill \$pids || true; sleep 1; fi; RECOVERY_AGENT_TOKEN=${quoted_token} RECOVERY_BRIDGE_UNIT=${quoted_bridge} RECOVERY_VLLM_UNIT=${quoted_vllm} RECOVERY_CHECKPOINT_SOURCE=${quoted_source} RECOVERY_CHECKPOINT_TARGET=${quoted_target} nohup ${quoted_python} ${quoted_dir}/remote_agent.py --port ${REMOTE_RECOVERY_PORT} --metrics-port ${REMOTE_RECOVERY_METRICS_PORT} > /tmp/remote-recovery-agent-${REMOTE_RECOVERY_PORT}.log 2>&1 < /dev/null & fi"
}

port_is_listening() {
  (command -v ss >/dev/null && ss -H -lnt "sport = :$1" | grep -q .) \
    || (command -v lsof >/dev/null && lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1)
}

ensure_tunnel() {
  local local_port="$1" remote_port="$2" probe_path="${3:-/metrics}"
  if port_is_listening "$local_port"; then
    if curl -fsS --max-time 2 "http://${LOCAL_BIND}:${local_port}${probe_path}" >/dev/null 2>&1; then
      log "tunnel ${LOCAL_BIND}:${local_port} already listening"
      return
    fi
    log "tunnel ${LOCAL_BIND}:${local_port} is stale; rebuilding"
    # The control master may have been created with the previous bridge port.
    for old_remote_port in "$remote_port" 8002 8010; do
      ssh -S "$SSH_SOCKET" -O cancel "${SSH_COMMON[@]}" \
        -L "${LOCAL_BIND}:${local_port}:127.0.0.1:${old_remote_port}" "$SSH_TARGET" \
        >/dev/null 2>&1 || true
    done
  fi
  log "forwarding ${LOCAL_BIND}:${local_port} -> remote 127.0.0.1:${remote_port}"
  ssh -S "$SSH_SOCKET" -fnNT "${SSH_COMMON[@]}" \
    -L "${LOCAL_BIND}:${local_port}:127.0.0.1:${remote_port}" "$SSH_TARGET"
}

wait_http() {
  local url="$1" label="$2"
  for _ in {1..20}; do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      log "${label}: up"
      return
    fi
    sleep 1
  done
  die "${label} is unreachable at ${url}"
}

start_control_master
sync_remote_recovery_agent
start_remote_bridge
start_remote_recovery_agent
ensure_tunnel 19100 9100
ensure_tunnel 18082 8082
ensure_tunnel 18010 "$REMOTE_BRIDGE_PORT"
ensure_tunnel 18004 "$REMOTE_RECOVERY_PORT" /healthz
ensure_tunnel 19004 "$REMOTE_RECOVERY_METRICS_PORT"

wait_http "http://${LOCAL_BIND}:18082/metrics" "NPU exporter"
wait_http "http://${LOCAL_BIND}:18010/metrics" "Agent bridge"
wait_http "http://${LOCAL_BIND}:18004/healthz" "Remote recovery agent"
wait_http "http://${LOCAL_BIND}:19100/metrics" "node exporter"

log "applying Prometheus scrape targets, rules and runtime resources"
kubectl apply -k "$ROOT_DIR/k8s"
log "applying Grafana dashboards"
kubectl apply -k "$ROOT_DIR/grafana"

log "monitoring started"
log "Prometheus targets: ${LOCAL_BIND}:19100, ${LOCAL_BIND}:18082, ${LOCAL_BIND}:18010, ${LOCAL_BIND}:19004"
log "remote bridge log: ${REMOTE_DIR}/../observability is remote; inspect /tmp/vllm-agent-bridge-${REMOTE_BRIDGE_PORT}.log"
