#!/bin/sh
set -eu

source_ref="https://github.com/pumpkinredbean/ProUse.git"
git_ref="main"

usage() {
  printf '%s\n' "Usage: sh install.sh [--source PATH_OR_GIT_URL] [--ref GIT_REF]"
  printf '%s\n' "Installs the prouse command with uv without changing ~/.prouse configuration."
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; source_ref=$2; shift 2 ;;
    --ref) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; git_ref=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf '%s\n' "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and rerun this command." >&2
  exit 1
fi
case "$source_ref" in
  http://*|https://*|ssh://*|git@*) package_source="git+$source_ref@$git_ref" ;;
  git+*) package_source="$source_ref@$git_ref" ;;
  *) package_source=$source_ref ;;
esac

uv tool install --force --from "$package_source" prouse

if command -v prouse >/dev/null 2>&1; then
  printf '%s\n' "Installed: $(command -v prouse)"
  prouse --version
else
  uv_bin_dir=$(uv tool dir --bin 2>/dev/null || true)
  printf '%s\n' "ProUse was installed, but prouse is not on PATH." >&2
  [ -z "$uv_bin_dir" ] || printf '%s\n' "Add this directory to PATH: $uv_bin_dir" >&2
  printf '%s\n' "You can also run: uv tool update-shell" >&2
  exit 1
fi
