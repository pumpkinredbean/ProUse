#!/bin/sh
set -eu

source_ref="https://github.com/pumpkinredbean/ProUse.git"
git_ref="main"
modify_path=1

usage() {
  printf '%s\n' "Usage: sh install.sh [--source PATH_OR_GIT_URL] [--ref GIT_REF] [--no-modify-path]"
  printf '%s\n' "Installs prouse and missing uv/Python prerequisites without changing ~/.prouse."
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; source_ref=$2; shift 2 ;;
    --ref) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; git_ref=$2; shift 2 ;;
    --no-modify-path) modify_path=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf '%s\n' "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

uv_command=$(command -v uv || true)
if [ -z "$uv_command" ]; then
  uv_install_dir="${UV_INSTALL_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}"
  if [ -x "$uv_install_dir/uv" ]; then
    uv_command="$uv_install_dir/uv"
  else
    printf '%s\n' "Installing uv from astral.sh..."
    uv_installer=$(mktemp "${TMPDIR:-/tmp}/prouse-uv.XXXXXX")
    trap 'rm -f "$uv_installer"' EXIT
    trap 'exit 130' INT TERM
    if command -v curl >/dev/null 2>&1; then
      curl --proto '=https' --tlsv1.2 -fsSL https://astral.sh/uv/install.sh -o "$uv_installer"
    elif command -v wget >/dev/null 2>&1; then
      wget -q https://astral.sh/uv/install.sh -O "$uv_installer"
    else
      printf '%s\n' "Install curl or wget, then run this installer again." >&2
      exit 1
    fi
    UV_INSTALL_DIR="$uv_install_dir" UV_NO_MODIFY_PATH=1 sh "$uv_installer"
    uv_command="$uv_install_dir/uv"
  fi
fi
case "$source_ref" in
  http://*|https://*|ssh://*|git@*) package_source="git+$source_ref@$git_ref" ;;
  git+*) package_source="$source_ref@$git_ref" ;;
  *) package_source=$source_ref ;;
esac

# Reinstall also refreshes a moving Git ref when the package version is unchanged.
"$uv_command" tool install --force --reinstall --python "${PROUSE_PYTHON:-3.13}" --from "$package_source" prouse

uv_bin_dir=$("$uv_command" tool dir --bin)
prouse_command="$uv_bin_dir/prouse"
if [ ! -x "$prouse_command" ]; then
  printf '%s\n' "Installation did not produce an executable at $prouse_command" >&2
  exit 1
fi
printf '\n%s\n' "Installed: $prouse_command"
"$prouse_command" --version

if [ "$(command -v prouse || true)" != "$prouse_command" ]; then
  if [ "$modify_path" -eq 1 ]; then
    "$uv_command" tool update-shell
  fi
  printf '\n%s\n' "For this terminal, add the tool directory to PATH: $uv_bin_dir"
  printf '%s\n' "Or run the installed executable above by its full path."
fi
printf '\n%s\n' "Next, from your project directory:"
printf '%s\n' "  prouse setup --workspace ." "  prouse admin"
