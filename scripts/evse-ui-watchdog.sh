#!/usr/bin/env bash
# Local health watchdog for evse-ui on Raspberry Pi.
# Intended for root cron every 10 minutes — see GUIDE.md (Health watchdog section).
#
# 1) Probes evse-ui /health on localhost (reboot after repeated failures — see below).
# 2) If HTTP is OK: optionally checks Tailscale is connected (BackendState + Self.Online when present);
#    on first failed check, restarts tailscaled (subject to cooldown). HTTP still uses 3×10s retries.
set -u
set -o pipefail

: "${EVSE_UI_PORT:=8080}"
: "${HTTP_TIMEOUT:=5}"
: "${GRACE_SEC:=600}"
: "${FAIL_STREAK_MAX:=3}"
: "${STREAK_FILE:=/run/evse-ui-watchdog/streak}"
: "${LOG_TAG:=evse-ui-watchdog}"

# Tailscale: set TAILSCALE_CHECK=0 to disable. Cooldown avoids restart loops.
: "${TAILSCALE_CHECK:=1}"
: "${TAILSCALE_RESTART_COOLDOWN_SEC:=1800}"
: "${TAILSCALE_STATE_DIR:=/run/evse-ui-watchdog}"
: "${TAILSCALE_LAST_RESTART_FILE:=${TAILSCALE_STATE_DIR}/tailscale_last_restart}"

log() {
  logger -t "$LOG_TAG" -- "$*"
}

log_debug() {
  logger -p user.debug -t "$LOG_TAG" -- "$*"
}

read_uptime_sec() {
  awk '{print int($1)}' /proc/uptime
}

read_streak() {
  local s=0
  if [[ -f "$STREAK_FILE" ]]; then
    s=$(<"$STREAK_FILE") || true
  fi
  [[ "$s" =~ ^[0-9]+$ ]] || s=0
  echo "$s"
}

write_streak() {
  mkdir -p "$(dirname "$STREAK_FILE")"
  echo "$1" >"$STREAK_FILE"
}

probe() {
  curl -sf --max-time "$HTTP_TIMEOUT" "http://127.0.0.1:${EVSE_UI_PORT}/health" >/dev/null
}

run_probes() {
  local attempt
  for attempt in 1 2 3; do
    if probe; then
      return 0
    fi
    if [[ "$attempt" -lt 3 ]]; then
      sleep 10
    fi
  done
  return 1
}

# True when Tailscale looks connected: BackendState must be Running; if JSON includes Self.Online, it must be true.
tailscale_connected_ok() {
  local json
  json=$(timeout 20 tailscale status --json 2>/dev/null) || return 1
  printf '%s' "$json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if d.get('BackendState') != 'Running':
        sys.exit(1)
    self = d.get('Self') or {}
    if 'Online' in self and not self.get('Online'):
        sys.exit(1)
    sys.exit(0)
except Exception:
    sys.exit(1)
"
}

maybe_fix_tailscale() {
  command -v tailscale >/dev/null 2>&1 || return 0
  [[ "${TAILSCALE_CHECK}" == "1" ]] || return 0

  if tailscale_connected_ok; then
    log_debug "tailscale connected (BackendState Running, Online OK if present)"
    return 0
  fi

  log "tailscale not connected or unhealthy (single check failed) — will try tailscaled restart if allowed"

  mkdir -p "$TAILSCALE_STATE_DIR"
  local now last gap
  now=$(date +%s)
  last=0
  if [[ -f "$TAILSCALE_LAST_RESTART_FILE" ]]; then
    last=$(<"$TAILSCALE_LAST_RESTART_FILE") || last=0
  fi
  [[ "$last" =~ ^[0-9]+$ ]] || last=0
  gap=$((now - last))

  if (( last > 0 && gap < TAILSCALE_RESTART_COOLDOWN_SEC )); then
    log "tailscale still unhealthy; last tailscaled restart ${gap}s ago (cooldown ${TAILSCALE_RESTART_COOLDOWN_SEC}s) — skipping"
    return 0
  fi

  if [[ -n "${EVSE_UI_DRY_RUN:-}" ]]; then
    log "EVSE_UI_DRY_RUN is set: would run systemctl restart tailscaled"
    return 0
  fi

  if ! systemctl is-active --quiet tailscaled 2>/dev/null; then
    log "tailscaled service not active; trying systemctl start tailscaled"
    systemctl start tailscaled || log "systemctl start tailscaled failed (exit $?)"
    echo "$(date +%s)" >"$TAILSCALE_LAST_RESTART_FILE"
    return 0
  fi

  log "restarting tailscaled"
  if systemctl restart tailscaled; then
    echo "$(date +%s)" >"$TAILSCALE_LAST_RESTART_FILE"
    log "tailscaled restart issued"
  else
    log "systemctl restart tailscaled failed (exit $?)"
  fi
}

uptime_sec=$(read_uptime_sec)
in_grace=0
if (( uptime_sec < GRACE_SEC )); then
  in_grace=1
fi

if run_probes; then
  write_streak 0
  log_debug "health OK (uptime ${uptime_sec}s, grace=${in_grace})"
  if (( ! in_grace )); then
    maybe_fix_tailscale
  else
    log_debug "boot grace: skipping tailscale maintenance"
  fi
  exit 0
fi

log "health FAIL after 3 attempts (10s apart); port=${EVSE_UI_PORT} uptime=${uptime_sec}s"

if (( in_grace )); then
  log "boot grace active (${GRACE_SEC}s): not counting toward reboot"
  exit 0
fi

streak=$(read_streak)
streak=$((streak + 1))
write_streak "$streak"
log "consecutive failed cron runs: ${streak}/${FAIL_STREAK_MAX}"

if (( streak >= FAIL_STREAK_MAX )); then
  log "threshold reached; rebooting"
  write_streak 0
  if [[ -n "${EVSE_UI_DRY_RUN:-}" ]]; then
    log "EVSE_UI_DRY_RUN is set: skipping shutdown -r now"
    exit 0
  fi
  /sbin/shutdown -r now "evse-ui watchdog: ${FAIL_STREAK_MAX} failed health checks"
fi
