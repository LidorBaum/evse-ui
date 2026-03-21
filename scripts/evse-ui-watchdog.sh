#!/usr/bin/env bash
# Local health watchdog for evse-ui on Raspberry Pi.
# Intended for root cron every 10 minutes — see GUIDE.md (Health watchdog section).
set -u
set -o pipefail

: "${EVSE_UI_PORT:=8080}"
: "${HTTP_TIMEOUT:=5}"
: "${GRACE_SEC:=600}"
: "${FAIL_STREAK_MAX:=3}"
: "${STREAK_FILE:=/run/evse-ui-watchdog/streak}"
: "${LOG_TAG:=evse-ui-watchdog}"

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

uptime_sec=$(read_uptime_sec)
in_grace=0
if (( uptime_sec < GRACE_SEC )); then
  in_grace=1
fi

if run_probes; then
  write_streak 0
  log_debug "health OK (uptime ${uptime_sec}s, grace=${in_grace})"
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
