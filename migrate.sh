#!/usr/bin/env bash
# migrate.sh — Push budget-app data from THIS machine to a target machine over SSH
#
# Run on the SOURCE (current/old) machine. The target must:
#   • Be reachable via SSH (Tailscale hostname, LAN IP, or regular hostname)
#   • Have the repo cloned and setup.sh already run
#   • Have Docker running
#
# What gets migrated:
#   • samples/          — all bank statements, paystubs, CSVs
#   • budget_context.md — your inferred financial profile
#   • budget.db         — the full SQLite database (Docker volume budget_app_data)
#
# Usage:
#   ./migrate.sh <target-host>                   # push to user@target-host (same username)
#   ./migrate.sh <target-host> --user alice      # push to alice@target-host
#   ./migrate.sh <target-host> --app-dir /path   # if app lives elsewhere on target
#   ./migrate.sh <target-host> --dry-run         # show what would happen, don't transfer
#
# Examples:
#   ./migrate.sh my-new-pc                       # Tailscale hostname
#   ./migrate.sh 100.64.0.42                     # Tailscale IP
#   ./migrate.sh 192.168.1.50                    # LAN IP

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET_HOST=""
TARGET_USER="$USER"
TARGET_APP_DIR="~/budget-app"
DRY_RUN=0
VOLUME_NAME="budget_app_data"
BACKUP_FILE="/tmp/budget_db_migration_$(date +%Y%m%d_%H%M%S).tar.gz"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

info()  { echo -e "${BLUE}[migrate]${NC} $*"; }
ok()    { echo -e "${GREEN}[ok]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
error() { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

usage() {
  echo "Usage: $0 <target-host> [--user <username>] [--app-dir <path>] [--dry-run]"
  exit 1
}

# ── Parse args ────────────────────────────────────────────────────────────────
[[ $# -eq 0 ]] && usage
TARGET_HOST="$1"; shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)     TARGET_USER="$2"; shift 2 ;;
    --app-dir)  TARGET_APP_DIR="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    *) error "Unknown argument: $1" ;;
  esac
done

TARGET="${TARGET_USER}@${TARGET_HOST}"
TARGET_RSYNC="${TARGET}:${TARGET_APP_DIR}"

cd "$SCRIPT_DIR"

echo ""
echo -e "${BLUE}══════════════════════════════════════════${NC}"
echo -e "${BLUE}   Budget App Migration${NC}"
echo -e "${BLUE}══════════════════════════════════════════${NC}"
echo ""
echo -e "  Source:  ${GREEN}$(hostname)${NC} ($SCRIPT_DIR)"
echo -e "  Target:  ${GREEN}${TARGET}${NC} (${TARGET_APP_DIR})"
[[ "$DRY_RUN" -eq 1 ]] && echo -e "  Mode:    ${YELLOW}DRY RUN — no data will be transferred${NC}"
echo ""

# ── 1. SSH connectivity check ─────────────────────────────────────────────────
info "Checking SSH connectivity to ${TARGET}..."
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$TARGET" "echo ok" &>/dev/null; then
  error "Cannot reach ${TARGET} via SSH.

  Troubleshooting:
    • Is Tailscale running on both machines?  Run: tailscale status
    • Is SSH running on target?               Run: sudo systemctl start ssh
    • Copy your SSH key:                      ssh-copy-id ${TARGET}
    • Try a manual connection:                ssh ${TARGET}"
fi
ok "SSH connection to ${TARGET} OK"

# ── 2. Check target has Docker ────────────────────────────────────────────────
info "Checking Docker on target..."
if ! ssh "$TARGET" "docker info" &>/dev/null; then
  error "Docker is not running on ${TARGET}. Run setup.sh on the target first."
fi
ok "Docker running on target"

# ── 3. Transfer samples/ ─────────────────────────────────────────────────────
SAMPLES_SIZE=$(du -sh samples/ 2>/dev/null | cut -f1 || echo "unknown")
info "Transferring samples/ (${SAMPLES_SIZE})..."

if [[ "$DRY_RUN" -eq 1 ]]; then
  rsync -avz --dry-run --progress \
    --exclude='.gitkeep' \
    samples/ "${TARGET_RSYNC}/samples/"
else
  rsync -avz --progress \
    --exclude='.gitkeep' \
    samples/ "${TARGET_RSYNC}/samples/"
fi
ok "samples/ transferred"

# ── 4. Transfer budget_context.md ────────────────────────────────────────────
if [[ -f budget_context.md ]]; then
  CONTEXT_SIZE=$(wc -c < budget_context.md)
  info "Transferring budget_context.md (${CONTEXT_SIZE} bytes)..."
  if [[ "$DRY_RUN" -eq 0 ]]; then
    rsync -avz budget_context.md "${TARGET_RSYNC}/budget_context.md"
  fi
  ok "budget_context.md transferred"
else
  warn "budget_context.md not found locally — skipping"
fi

# ── 5. Export Docker volume (budget.db) ───────────────────────────────────────
info "Checking Docker volume '${VOLUME_NAME}' on source..."
if ! docker volume inspect "$VOLUME_NAME" &>/dev/null; then
  warn "Docker volume '${VOLUME_NAME}' not found. Is the app running? Skipping DB migration."
else
  DB_SIZE=$(docker run --rm -v "${VOLUME_NAME}:/data" alpine sh -c "du -sh /data 2>/dev/null | cut -f1" 2>/dev/null || echo "unknown")
  info "Exporting database volume (${DB_SIZE}) to ${BACKUP_FILE}..."

  if [[ "$DRY_RUN" -eq 0 ]]; then
    docker run --rm \
      -v "${VOLUME_NAME}:/data:ro" \
      -v "/tmp:/backup" \
      alpine \
      tar czf "/backup/$(basename "$BACKUP_FILE")" -C /data .

    ok "Volume exported to ${BACKUP_FILE}"

    info "Transferring database backup to target..."
    rsync -avz --progress "$BACKUP_FILE" "${TARGET}:/tmp/"

    REMOTE_BACKUP="/tmp/$(basename "$BACKUP_FILE")"
    info "Restoring database on target..."
    ssh "$TARGET" bash << ENDSSH
set -e
# Ensure the volume exists
docker volume create ${VOLUME_NAME} >/dev/null 2>&1 || true

# Stop backend to avoid write conflicts during restore
cd ${TARGET_APP_DIR} && docker compose stop backend 2>/dev/null || true

# Restore the volume
docker run --rm \
  -v ${VOLUME_NAME}:/data \
  -v /tmp:/backup \
  alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/$(basename "$BACKUP_FILE") -C /data"

# Restart
cd ${TARGET_APP_DIR} && docker compose start backend

echo "restore_done"
ENDSSH

    ok "Database restored on target"

    # Cleanup
    info "Cleaning up temporary backup files..."
    rm -f "$BACKUP_FILE"
    ssh "$TARGET" "rm -f ${REMOTE_BACKUP}"
    ok "Cleanup done"
  else
    info "[dry-run] Would export volume and transfer to ${TARGET}:/tmp/"
  fi
fi

# ── 6. Summary ────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════${NC}"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo -e "${YELLOW}   Dry run complete — nothing was transferred${NC}"
else
  echo -e "${GREEN}   Migration complete!${NC}"
fi
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo ""
if [[ "$DRY_RUN" -eq 0 ]]; then
  echo "Verify on the target machine:"
  echo "  ssh ${TARGET} 'cd ${TARGET_APP_DIR} && docker compose ps'"
  echo "  http://${TARGET_HOST}:8081   ← frontend"
  echo ""
fi
