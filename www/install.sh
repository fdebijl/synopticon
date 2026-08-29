#!/usr/bin/env bash
#
# Synopticon installer — https://github.com/fdebijl/synopticon
#
#   curl -fsSL https://fdebijl.github.io/synopticon/install.sh | bash
#
# Everything runs from main() at the very bottom, so a truncated download can
# never execute a half-written script.

if [ -z "${BASH_VERSION:-}" ]; then
  echo "This installer needs bash, not sh. Re-run it as:" >&2
  echo "  curl -fsSL https://fdebijl.github.io/synopticon/install.sh | bash" >&2
  exit 1
fi

set -euo pipefail

REPO_URL="https://github.com/fdebijl/synopticon"
TARGET_DIR="synopticon"
PORT=8686
ASSUME_YES=0
START_WEB=1
INSTALL_UV=1

# Free space for the venv (~2 GB), node_modules (~0.5 GB), the ONNX weights
# (~1.5 GB) and enough head-room for face crops and the originals cache.
DISK_WARN_GB=10
DISK_FAIL_GB=4
RAM_WARN_GB=4

# Newline-joined rather than arrays: macOS still ships bash 3.2, where
# ${#arr[@]} on an empty array trips `set -u`.
FAILURES=""
WARNINGS=""
FAIL_COUNT=0
WARN_COUNT=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'; C_GREEN=$'\033[32m'; C_BLUE=$'\033[34m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_YELLOW=""; C_GREEN=""; C_BLUE=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$C_BLUE" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
ok()   { printf '  %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
note() { printf '  %s·%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

fail_check() { FAILURES="${FAILURES}${1}"$'\n'; FAIL_COUNT=$((FAIL_COUNT + 1)); }
warn_check() { WARNINGS="${WARNINGS}${1}"$'\n'; WARN_COUNT=$((WARN_COUNT + 1)); }

have() { command -v "$1" >/dev/null 2>&1; }

# stdin is the script itself under `curl | bash`, so prompts must come from the
# terminal or not at all.
confirm() {
  local prompt="$1" default="${2:-n}" reply
  if [ "$ASSUME_YES" = 1 ]; then return 0; fi
  if [ ! -r /dev/tty ]; then
    [ "$default" = "y" ]
    return
  fi
  printf '%s [%s] ' "$prompt" "$([ "$default" = y ] && echo "Y/n" || echo "y/N")" > /dev/tty
  read -r reply < /dev/tty || reply=""
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy] ]]
}

usage() {
  cat <<EOF
Synopticon installer

Usage: install.sh [options]

  --dir DIR      Install into DIR (default: ./${TARGET_DIR})
  --port PORT    Port to check and serve on (default: ${PORT})
  --yes, -y      Answer yes to every prompt (non-interactive)
  --no-start     Install only; don't launch the web GUI at the end
  --no-uv        Never install uv; fail if it is missing
  --help, -h     Show this message
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dir)      TARGET_DIR="${2:-}"; [ -n "$TARGET_DIR" ] || die "--dir needs a value"; shift 2 ;;
      --port)     PORT="${2:-}"
                  [[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] \
                    || die "--port needs a number between 1 and 65535"
                  shift 2 ;;
      -y|--yes)   ASSUME_YES=1; shift ;;
      --no-start) START_WEB=0; shift ;;
      --no-uv)    INSTALL_UV=0; shift ;;
      -h|--help)  usage; exit 0 ;;
      *)          usage >&2; die "unknown option: $1" ;;
    esac
  done
}

# --- preflight ---------------------------------------------------------------

check_platform() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"

  case "$os" in
    Linux)  ok "Platform: Linux ($arch)" ;;
    Darwin) ok "Platform: macOS ($arch)" ;;
    *)      fail_check "Unsupported OS: $os. Synopticon installs on Linux and macOS; on Windows use WSL2 or the Docker image." ;;
  esac

  case "$arch" in
    x86_64|amd64|arm64|aarch64) ;;
    *) fail_check "Unsupported CPU architecture: $arch. onnxruntime publishes wheels for x86_64 and arm64 only." ;;
  esac

  # onnxruntime and opencv ship manylinux wheels only — musl builds resolve but
  # fail to import, and pip falls back to a source build that will not succeed.
  if [ "$os" = "Linux" ] && { [ -f /etc/alpine-release ] || ldd --version 2>&1 | grep -qi musl; }; then
    fail_check "This looks like a musl system (Alpine). onnxruntime and OpenCV publish no musl wheels — use a glibc distro or the Docker image ($REPO_URL#docker-images)."
  fi

  if [ "$(id -u)" = "0" ]; then
    warn_check "Running as root. The checkout, the venv and the npm cache will all be root-owned; prefer an unprivileged user."
  fi

  if [ ! -w "${HOME:-/nonexistent}" ]; then
    fail_check "\$HOME (${HOME:-unset}) is not writable — uv and npm both need it for their caches."
  fi
}

check_git() {
  if have git; then
    ok "git $(git --version | awk '{print $3}')"
  elif [ "$(uname -s)" = "Darwin" ]; then
    fail_check "git not found. Install the Xcode command line tools: xcode-select --install"
  else
    fail_check "git not found. Install it with your package manager (apt install git / dnf install git / pacman -S git)."
  fi
}

check_node() {
  if ! have node; then
    fail_check "Node.js not found. Synopticon builds its web GUI with Vite, which needs Node 22.12+ (20.19+ also works). Install it from https://nodejs.org, via nvm (https://github.com/nvm-sh/nvm), or your package manager."
    return
  fi

  local raw major minor
  raw="$(node -v 2>/dev/null || echo v0.0.0)"
  raw="${raw#v}"
  major="${raw%%.*}"
  minor="${raw#*.}"; minor="${minor%%.*}"
  [[ "$major" =~ ^[0-9]+$ ]] || major=0
  [[ "$minor" =~ ^[0-9]+$ ]] || minor=0

  # Vite 8 accepts ^20.19.0 || >=22.12.0 — 21.x and 22.0–22.11 are rejected
  # outright, so catch them here rather than during `npm run build`.
  if [ "$major" -ge 23 ] || { [ "$major" = 22 ] && [ "$minor" -ge 12 ]; }; then
    ok "Node v$raw"
  elif [ "$major" = 20 ] && [ "$minor" -ge 19 ]; then
    ok "Node v$raw"
    warn_check "Node v$raw works, but the README targets Node 22+; upgrade when convenient."
  else
    fail_check "Node v$raw cannot build the frontend: Vite 8 accepts 20.19+ or 22.12+, and nothing in between. Upgrade via nvm: nvm install 22 && nvm use 22"
  fi

  if have npm; then
    ok "npm $(npm -v 2>/dev/null || echo '?')"
  else
    fail_check "npm not found. It normally ships with Node — reinstall Node, or install your distro's nodejs-npm package."
  fi
}

check_python() {
  # uv provisions its own interpreter when the system one is unusable, so a
  # missing or wrong-versioned python3 is informational, not fatal.
  if ! have python3; then
    note "No system python3 — uv will download a managed Python 3.12 during sync."
    return
  fi

  local raw major minor
  raw="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)"
  major="${raw%%.*}"; minor="${raw#*.}"
  [[ "$major" =~ ^[0-9]+$ ]] || major=0
  [[ "$minor" =~ ^[0-9]+$ ]] || minor=0

  if [ "$major" = 3 ] && [ "$minor" -ge 11 ] && [ "$minor" -le 12 ]; then
    ok "Python $raw"
  else
    note "Python $raw is outside the supported 3.11–3.12 range — uv will download a managed Python 3.12 instead."
  fi
}

check_uv() {
  if have uv; then
    ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
    return
  fi
  if [ "$INSTALL_UV" = 0 ]; then
    fail_check "uv not found and --no-uv was passed. Install it from https://docs.astral.sh/uv/getting-started/installation/"
    return
  fi
  note "uv not found — the installer will offer to install it."
}

check_downloader() {
  if have curl || have wget; then
    ok "Downloader available"
  else
    fail_check "Neither curl nor wget found; uv and the model downloader both need one."
  fi
}

check_network() {
  local host="github.com"
  if have curl; then
    if curl -fsS --max-time 15 -o /dev/null "https://$host" 2>/dev/null; then
      ok "Network reachable"
    else
      fail_check "Cannot reach https://$host. Check your connection, DNS, or HTTPS_PROXY setting."
    fi
  elif have wget; then
    if wget -q --timeout=15 --spider "https://$host" 2>/dev/null; then
      ok "Network reachable"
    else
      fail_check "Cannot reach https://$host. Check your connection, DNS, or HTTPS_PROXY setting."
    fi
  fi
}

check_disk() {
  local parent avail_kb avail_gb
  parent="$(dirname -- "$TARGET_DIR")"
  [ -d "$parent" ] || parent="."
  avail_kb="$(df -Pk -- "$parent" 2>/dev/null | awk 'NR==2 {print $4}')" || avail_kb=""
  [[ "$avail_kb" =~ ^[0-9]+$ ]] || { note "Could not determine free disk space"; return; }
  avail_gb=$(( avail_kb / 1024 / 1024 ))

  if [ "$avail_gb" -lt "$DISK_FAIL_GB" ]; then
    fail_check "Only ${avail_gb} GB free on $(cd "$parent" && pwd). The venv, node_modules and the ONNX weights need roughly 4 GB before your library is touched."
  elif [ "$avail_gb" -lt "$DISK_WARN_GB" ]; then
    warn_check "Only ${avail_gb} GB free. Face crops and the originals cache grow with your library — ${DISK_WARN_GB} GB+ is a comfortable start."
  else
    ok "Disk: ${avail_gb} GB free"
  fi
}

check_memory() {
  local total_kb=0 total_gb
  if [ -r /proc/meminfo ]; then
    total_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
  elif have sysctl; then
    local bytes
    bytes="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
    total_kb=$(( bytes / 1024 ))
  fi
  [[ "$total_kb" =~ ^[0-9]+$ ]] && [ "$total_kb" -gt 0 ] || return 0
  total_gb=$(( total_kb / 1024 / 1024 ))

  if [ "$total_gb" -lt "$RAM_WARN_GB" ]; then
    warn_check "${total_gb} GB RAM detected. The frontend type-check and the face pipeline are both memory-hungry; ${RAM_WARN_GB} GB+ is recommended."
  else
    ok "Memory: ${total_gb} GB"
  fi
}

check_port() {
  if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    exec 3>&- 2>/dev/null || true
    warn_check "Something is already listening on port $PORT. Start the GUI with: uv run synopticon web --port <other>"
  else
    ok "Port $PORT is free"
  fi
}

check_target_dir() {
  if [ -e "$TARGET_DIR" ]; then
    if [ -d "$TARGET_DIR/.git" ] && git -C "$TARGET_DIR" remote get-url origin 2>/dev/null | grep -q "synopticon"; then
      note "$TARGET_DIR is already a Synopticon checkout — it will be reused, not re-cloned."
    elif [ -d "$TARGET_DIR" ] && [ -z "$(ls -A -- "$TARGET_DIR" 2>/dev/null)" ]; then
      note "$TARGET_DIR exists but is empty — cloning into it."
    else
      fail_check "$TARGET_DIR already exists and is not a Synopticon checkout. Move it aside, or pass --dir <other>."
    fi
  fi
}

preflight() {
  step "Checking prerequisites"
  check_platform
  check_git
  check_node
  check_python
  check_uv
  check_downloader
  check_network
  check_disk
  check_memory
  check_port
  check_target_dir

  if [ "$WARN_COUNT" -gt 0 ]; then
    printf '\n%swarnings:%s\n' "$C_YELLOW" "$C_RESET"
    printf '%s' "$WARNINGS" | while IFS= read -r line; do
      if [ -n "$line" ]; then printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$line"; fi
    done
  fi

  if [ "$FAIL_COUNT" -gt 0 ]; then
    printf '\n%sblockers:%s\n' "$C_RED" "$C_RESET"
    printf '%s' "$FAILURES" | while IFS= read -r line; do
      if [ -n "$line" ]; then printf '  %sx%s %s\n' "$C_RED" "$C_RESET" "$line"; fi
    done
    printf '\nFix the blockers above and re-run the installer.\n' >&2
    exit 1
  fi

  if [ "$WARN_COUNT" -gt 0 ]; then
    confirm $'\nContinue anyway?' y || die "Aborted."
  fi
}

# --- install -----------------------------------------------------------------

ensure_uv() {
  if have uv; then return 0; fi

  step "Installing uv"
  confirm "uv (the Python package manager Synopticon uses) is missing. Install it from https://astral.sh/uv?" y \
    || die "uv is required. See https://docs.astral.sh/uv/getting-started/installation/"

  if have curl; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  else
    wget -qO- https://astral.sh/uv/install.sh | sh
  fi

  # The installer writes to ~/.local/bin, which is not necessarily on PATH yet.
  if [ -f "$HOME/.local/bin/env" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.local/bin/env"
  fi
  have uv || export PATH="$HOME/.local/bin:$PATH"
  have uv || die "uv installed but is not on PATH. Open a new shell and re-run this installer."
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
}

fetch_repo() {
  step "Fetching Synopticon"
  if [ -d "$TARGET_DIR/.git" ]; then
    note "Reusing existing checkout at $TARGET_DIR"
    if confirm "Pull the latest changes?" y; then
      git -C "$TARGET_DIR" pull --ff-only \
        || printf '  %s!%s git pull failed; continuing with the checkout as-is.\n' "$C_YELLOW" "$C_RESET"
    fi
  else
    git clone "$REPO_URL" "$TARGET_DIR"
  fi
  cd "$TARGET_DIR" || die "Cannot enter $TARGET_DIR"
  ok "Checkout at $(pwd)"
}

prepare_dirs() {
  step "Preparing data and model directories"
  mkdir -p data models
  if [ -f data/config.toml ]; then
    note "data/config.toml already exists — leaving it untouched."
  else
    cp config.example.toml data/config.toml
    ok "Wrote data/config.toml from the example"
  fi
}

install_python_deps() {
  step "Installing Python dependencies (this takes a few minutes)"
  # cpu/gpu share an import namespace and are declared conflicting; pick one.
  # Swap --extra cpu for --extra gpu on an NVIDIA host with a working driver.
  uv sync --extra cpu --extra review --extra faiss \
    || die "uv sync failed. Re-run with 'uv sync --extra cpu --extra review --extra faiss' to see the full output."
  ok "Python environment ready"
}

build_frontend() {
  step "Building the web GUI"
  [ -f frontend/package-lock.json ] || die "frontend/package-lock.json is missing — the checkout looks incomplete."
  ( cd frontend && npm ci && npm run build ) \
    || die "The frontend build failed. Re-run it with 'cd frontend && npm ci && npm run build' to see the full output."
  [ -d src/synopticon/web/dist ] || die "The build produced no src/synopticon/web/dist — 'synopticon web' would serve nothing."
  ok "Web GUI built"
}

start_web() {
  cat <<EOF

${C_GREEN}${C_BOLD}Synopticon is installed.${C_RESET}

  Checkout   $(pwd)
  Config     data/config.toml
  GUI        http://localhost:${PORT}

Next: the GUI's first-run wizard walks you through connecting to your NAS and
downloading the face-recognition models. Start it any time with:

  cd $(pwd) && uv run synopticon web

EOF

  if [ "$START_WEB" = 0 ]; then
    return 0
  fi
  if ! confirm "Start the web GUI now?" y; then
    return 0
  fi

  say "Starting on http://localhost:${PORT} — press Ctrl-C to stop."
  exec uv run synopticon web --port "$PORT"
}

main() {
  parse_args ${1+"$@"}
  printf '%s%sSynopticon installer%s\n' "$C_BOLD" "$C_BLUE" "$C_RESET"
  preflight
  ensure_uv
  fetch_repo
  prepare_dirs
  install_python_deps
  build_frontend
  start_web
}

main ${1+"$@"}
