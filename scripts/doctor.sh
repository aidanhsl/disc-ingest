#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

failures=0
pass() { printf '[OK] %s\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1" >&2; failures=$((failures + 1)); }

if [[ "$(uname -s)" == "Linux" ]]; then pass 'Linux host detected'; else fail 'Optical passthrough requires Linux'; fi
if command -v docker >/dev/null 2>&1; then pass 'Docker command found'; else fail 'Docker is not installed or not in PATH'; fi
if docker compose version >/dev/null 2>&1; then pass 'Docker Compose v2 found'; else fail 'Docker Compose v2 is unavailable'; fi
if [[ -f .env ]]; then pass '.env exists'; else fail 'Copy .env.example to .env and edit it'; fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a

  for device in "${OPTICAL_BLOCK_DEVICE:-}" "${OPTICAL_GENERIC_DEVICE:-}"; do
    if [[ -n "$device" && -e "$device" ]]; then pass "$device exists"; else fail "Missing device: ${device:-not configured}"; fi
  done

  if [[ -n "${MEDIA_DIR:-}" && -d "$MEDIA_DIR" && -w "$MEDIA_DIR" ]]; then
    pass "$MEDIA_DIR exists and is writable"
  else
    fail "MEDIA_DIR does not exist or is not writable: ${MEDIA_DIR:-not configured}"
  fi
fi

if (( failures > 0 )); then
  printf '\nDoctor found %d problem(s).\n' "$failures" >&2
  exit 1
fi
printf '\nEverything required for startup looks good.\n'

