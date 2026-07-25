#!/usr/bin/env bash
# setup.sh — Bootstrap budget-app on a fresh machine
#
# Run as the app user (not root) in the repo directory.
# What it does:
#   1. Installs Docker + Docker Compose plugin if missing (Ubuntu/Debian)
#   2. Optionally installs Tailscale
#   3. Verifies claude / opencode CLI tools are on PATH (needed for LLM categorization)
#   4. Creates samples/ placeholder and empty budget_context.md if missing
#   5. Builds images and starts the stack
#
# Usage:
#   ./setup.sh                  # standard setup
#   ./setup.sh --with-tailscale # also install Tailscale
#   ./setup.sh --skip-docker    # skip Docker install (already installed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_TAILSCALE=0
SKIP_DOCKER=0

for arg in "$@"; do
  case "$arg" in
    --with-tailscale) WITH_TAILSCALE=1 ;;
    --skip-docker)    SKIP_DOCKER=1 ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

info()  { echo -e "${BLUE}[setup]${NC} $*"; }
ok()    { echo -e "${GREEN}[ok]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
error() { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

cd "$SCRIPT_DIR"
info "Working directory: $SCRIPT_DIR"

# ── 1. Docker ────────────────────────────────────────────────────────────────
if [[ "$SKIP_DOCKER" -eq 0 ]]; then
  if command -v docker &>/dev/null; then
    ok "Docker already installed: $(docker --version)"
  else
    info "Installing Docker..."
    if ! command -v apt-get &>/dev/null; then
      error "Auto-install only supports Ubuntu/Debian. Install Docker manually: https://docs.docker.com/engine/install/"
    fi
    sudo apt-get update -qq
    sudo apt-get install -y ca-certificates curl gnupg lsb-release
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    sudo usermod -aG docker "$USER"
    ok "Docker installed. NOTE: log out and back in for group membership to take effect."
    warn "If 'docker compose' fails, run: newgrp docker"
  fi

  # Ensure docker compose v2 is available
  if ! docker compose version &>/dev/null; then
    error "Docker Compose plugin not found. Install it: https://docs.docker.com/compose/install/"
  fi
  ok "Docker Compose: $(docker compose version --short)"
fi

# ── 2. Tailscale (optional) ──────────────────────────────────────────────────
if [[ "$WITH_TAILSCALE" -eq 1 ]]; then
  if command -v tailscale &>/dev/null; then
    ok "Tailscale already installed: $(tailscale version | head -1)"
  else
    info "Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
    ok "Tailscale installed. Run: sudo tailscale up"
    warn "After 'tailscale up', authenticate in the browser and note this machine's Tailscale hostname."
  fi
fi

# ── 3. LLM CLI tools ─────────────────────────────────────────────────────────
MISSING_LLMS=()
if ! command -v claude &>/dev/null; then
  MISSING_LLMS+=("claude")
fi
if ! command -v opencode &>/dev/null; then
  MISSING_LLMS+=("opencode")
fi

if [[ ${#MISSING_LLMS[@]} -eq 0 ]]; then
  ok "LLM tools found: claude + opencode"
else
  warn "Missing LLM CLI tools: ${MISSING_LLMS[*]}"
  warn "The app will start but LLM categorization will be disabled until these are installed."
  warn "  claude:   https://claude.ai/download  (Claude Code CLI)"
  warn "  opencode: https://opencode.ai"
fi

# ── 4. Data directories ───────────────────────────────────────────────────────
mkdir -p samples/paystubs

if [[ ! -f budget_context.md ]]; then
  cat > budget_context.md << 'EOF'
# Budget Context
_This file is auto-populated when you first import transactions._
EOF
  ok "Created empty budget_context.md"
else
  ok "budget_context.md already present"
fi

# ── 5. Build and start ────────────────────────────────────────────────────────
info "Building Docker images (this takes a minute on first run)..."
docker compose build

info "Starting services..."
docker compose up -d

info "Waiting for backend health check..."
attempts=0
until docker compose exec -T backend python3 -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" \
  &>/dev/null; do
  attempts=$((attempts + 1))
  if [[ $attempts -ge 30 ]]; then
    warn "Backend did not become healthy within 60s. Check logs: docker compose logs backend"
    break
  fi
  sleep 2
done

echo ""
ok "Budget app is running!"
echo ""
echo -e "  Frontend: ${GREEN}http://localhost:8081${NC}"
echo -e "  Backend:  ${GREEN}http://localhost:8001/api/health${NC}"
echo ""
echo "Next steps:"
echo "  • Copy your bank statement CSVs into samples/"
echo "  • Run migrate.sh from your old machine to transfer existing data"
echo "  • Or use the UI to import fresh statements"
echo ""
echo "Useful commands:"
echo "  docker compose logs -f       # follow logs"
echo "  docker compose down          # stop"
echo "  docker compose up -d         # start again"
