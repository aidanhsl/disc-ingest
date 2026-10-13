#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'This stack requires a Linux Docker host for direct optical-drive passthrough.\n' >&2
  exit 1
fi

declare -a block_devices=()

if command -v lsscsi >/dev/null 2>&1; then
  while IFS= read -r device; do
    [[ -n "$device" ]] && block_devices+=("$device")
  done < <(lsscsi -g | awk '$2 == "cd/dvd" {print $(NF-1)}')
else
  while IFS= read -r device; do
    [[ -n "$device" ]] && block_devices+=("$device")
  done < <(find /sys/class/block -maxdepth 1 -type l -name 'sr*' -printf '/dev/%f\n' 2>/dev/null | sort)
fi

if (( ${#block_devices[@]} == 0 )); then
  printf 'No optical drive was found. Plug it in, then run this script again.\n' >&2
  printf 'Installing lsscsi may improve detection: sudo apt install lsscsi\n' >&2
  exit 1
fi

for block_device in "${block_devices[@]}"; do
  generic_device=""

  if command -v lsscsi >/dev/null 2>&1; then
    generic_device="$(lsscsi -g | awk -v wanted="$block_device" '$2 == "cd/dvd" && $(NF-1) == wanted {print $NF; exit}')"
  fi

  if [[ -z "$generic_device" || "$generic_device" == "-" ]]; then
    device_name="${block_device##*/}"
    generic_name="$(find "/sys/class/block/$device_name/device/scsi_generic" -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null | head -n 1 || true)"
    [[ -n "$generic_name" ]] && generic_device="/dev/$generic_name"
  fi

  if [[ -z "$generic_device" || ! -e "$generic_device" ]]; then
    printf 'Found %s, but could not find its matching /dev/sgN device.\n' "$block_device" >&2
    printf 'Install lsscsi and run: lsscsi -g\n' >&2
    continue
  fi

  block_gid="$(stat -c '%g' "$block_device")"
  generic_gid="$(stat -c '%g' "$generic_device")"
  if [[ "$block_gid" == "$generic_gid" ]]; then
    supplementary_groups="$block_gid"
  else
    supplementary_groups="$block_gid,$generic_gid"
  fi

  printf 'Detected optical drive:\n'
  printf '  OPTICAL_BLOCK_DEVICE=%s\n' "$block_device"
  printf '  OPTICAL_GENERIC_DEVICE=%s\n' "$generic_device"
  printf '  SUP_GROUP_IDS=%s\n' "$supplementary_groups"
  printf '\nCopy these values into .env.\n'
done

