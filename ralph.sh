#!/bin/bash

# Ralph Wiggum Autonomous Development Loop
# =========================================
# This script runs Claude Code in a continuous loop, each iteration with a fresh
# context window. It reads PROMPT.md and feeds it to Claude until all tasks are
# complete or max iterations is reached.
#
# Usage: ./ralph.sh <max_iterations>
# Example: ./ralph.sh 20

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Check for required argument
if [ -z "$1" ]; then
  echo -e "${RED}Error: Missing required argument${NC}"
  echo ""
  echo "Usage: $0 <max_iterations>"
  echo "Example: $0 20"
  exit 1
fi

MAX_ITERATIONS=$1

# Verify required files exist
if [ ! -f "PROMPT.md" ]; then
  echo -e "${RED}Error: PROMPT.md not found${NC}"
  echo "Please create PROMPT.md or run /create-prd first"
  exit 1
fi

if [ ! -f "prd.md" ]; then
  echo -e "${RED}Error: prd.md not found${NC}"
  echo "Please create your PRD or run /create-prd first"
  exit 1
fi

if [ ! -f "activity.md" ]; then
  echo -e "${YELLOW}Warning: activity.md not found, creating it...${NC}"
  cat > activity.md << 'EOF'
# Project Build - Activity Log

## Current Status
**Last Updated:** Not started
**Tasks Completed:** 0
**Current Task:** None

---

## Session Log

<!-- Agent will append dated entries here -->
EOF
fi

# Create screenshots directory if it doesn't exist
mkdir -p screenshots

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}   Ralph Wiggum Autonomous Loop${NC}"
echo -e "${BLUE}======================================${NC}"
echo ""
echo -e "Max iterations: ${GREEN}$MAX_ITERATIONS${NC}"
echo -e "Completion signal: ${GREEN}<promise>COMPLETE</promise>${NC}"
echo ""
echo -e "${YELLOW}Starting in 3 seconds... Press Ctrl+C to abort${NC}"
sleep 3
echo ""

# Main loop
for ((i=1; i<=MAX_ITERATIONS; i++)); do
  echo -e "${BLUE}======================================${NC}"
  echo -e "${BLUE}   Iteration $i of $MAX_ITERATIONS${NC}"
  echo -e "${BLUE}======================================${NC}"
  echo ""

  # Run the iteration via OpenCode by default; fall back to Claude Code if
  # OpenCode is missing, errors out, or hits a credit/quota/auth failure.
  # Each iteration starts fresh with OpenCode, so a transient credit issue
  # in iteration N doesn't lock us out of OpenCode for iteration N+1.
  #
  # Model selection for OpenCode: $OPENCODE_MODEL or ~/.config/opencode/opencode.json default.
  # Model selection for Claude: whatever `claude` is configured to use.
  prompt_text="$(cat PROMPT.md)"
  result=""
  opencode_rc=0
  agent_used=""

  # Escalation: after $ESCALATE_AFTER consecutive iterations with no passes-flip
  # in prd.md, force-switch to `claude -p --model claude-opus-4-7`. This is the
  # "stuck on a hard story" trapdoor — opencode handles 80% of the work fast,
  # but when it loops on infrastructure walls (Playwright config, visual judge,
  # E2E plumbing) Opus's stronger orchestration is worth the credit cost.
  # Counter is file-based so it survives ralph.sh restarts; can be pre-seeded
  # by the outer supervisor when manually intervening (echo N > the file).
  ESCALATE_AFTER="${ESCALATE_AFTER:-5}"
  NO_FLIP_FILE="${NO_FLIP_FILE:-/tmp/.ralph-no-flip-count}"
  no_flip_count=$(cat "$NO_FLIP_FILE" 2>/dev/null || echo 0)
  passes_before=$(grep -c '"passes": true' prd.md 2>/dev/null || echo 0)
  escalate=0
  if [ "$no_flip_count" -ge "$ESCALATE_AFTER" ]; then
    escalate=1
    echo -e "${YELLOW}ESCALATING: ${no_flip_count} consecutive no-flip iters → forcing claude --model claude-opus-4-7 for this iteration${NC}" >&2
  fi

  # Sticky opencode-disabled sentinel. opencode can silently park for hours
  # waiting on a free-tier rate-limit retry-after (observed 57337s = ~16h).
  # When a rate-limit signal is detected, future iterations skip opencode
  # entirely for OPENCODE_BACKOFF_SECONDS (default 1h) and go straight to
  # claude — saves the per-iter timeout dead-time.
  OPENCODE_DISABLED_FILE="${OPENCODE_DISABLED_FILE:-/tmp/.ralph-opencode-disabled-until}"
  OPENCODE_BACKOFF_SECONDS="${OPENCODE_BACKOFF_SECONDS:-3600}"
  OPENCODE_ITER_TIMEOUT="${OPENCODE_ITER_TIMEOUT:-3600}"   # hard cap per opencode iter (60 min — back-half stories like Sankey/E2E take longer)
  opencode_disabled=0
  if [ -f "$OPENCODE_DISABLED_FILE" ]; then
    until_ts=$(cat "$OPENCODE_DISABLED_FILE" 2>/dev/null || echo 0)
    now_ts=$(date +%s)
    if [ "$now_ts" -lt "$until_ts" ]; then
      opencode_disabled=1
      remaining=$((until_ts - now_ts))
      echo -e "${YELLOW}opencode disabled ${remaining}s longer (sticky after prior rate-limit). Using claude.${NC}" >&2
    else
      rm -f "$OPENCODE_DISABLED_FILE"
    fi
  fi

  if [ $escalate -eq 0 ] && [ $opencode_disabled -eq 0 ] && command -v opencode >/dev/null 2>&1; then
    if [ -n "${OPENCODE_MODEL:-}" ]; then
      result=$(timeout "$OPENCODE_ITER_TIMEOUT" opencode run -m "$OPENCODE_MODEL" "$prompt_text" 2>&1) || true
    else
      result=$(timeout "$OPENCODE_ITER_TIMEOUT" opencode run "$prompt_text" 2>&1) || true
    fi
    opencode_rc=$?
  fi

  # Detect "OpenCode unusable" — disabled, missing binary, non-zero exit, hung-then-timed-out, or credit/quota/auth signals.
  opencode_failed=0
  rate_limited=0
  if [ $escalate -eq 1 ]; then
    opencode_failed=1
  elif [ $opencode_disabled -eq 1 ]; then
    opencode_failed=1
  elif ! command -v opencode >/dev/null 2>&1; then
    opencode_failed=1
    echo -e "${YELLOW}opencode not on PATH${NC}" >&2
  elif [ $opencode_rc -eq 124 ]; then
    opencode_failed=1
    rate_limited=1
    echo -e "${YELLOW}opencode timed out after ${OPENCODE_ITER_TIMEOUT}s (likely hung on rate-limit retry)${NC}" >&2
  elif [ $opencode_rc -ne 0 ]; then
    opencode_failed=1
    echo -e "${YELLOW}opencode exited rc=$opencode_rc${NC}" >&2
  elif echo "$result" | grep -qiE 'insufficient.{0,5}(credit|fund|balance|token)|out.{0,5}of.{0,5}(credit|token)|quota.{0,30}exceed|rate.{0,5}limit|payment.{0,5}required|\b40[12]\b|invalid.{0,5}api.{0,5}key|billing.{0,20}(issue|error|required)|FreeUsageLimit'; then
    opencode_failed=1
    rate_limited=1
    echo -e "${YELLOW}opencode output indicates credit/quota/auth failure${NC}" >&2
  fi

  # Sticky backoff after rate-limit signal: avoid retrying opencode for OPENCODE_BACKOFF_SECONDS.
  if [ $rate_limited -eq 1 ] && [ $opencode_disabled -eq 0 ]; then
    echo "$(($(date +%s) + OPENCODE_BACKOFF_SECONDS))" > "$OPENCODE_DISABLED_FILE"
    echo -e "${YELLOW}opencode disabled for ${OPENCODE_BACKOFF_SECONDS}s; subsequent iters will use claude directly.${NC}" >&2
  fi

  if [ $opencode_failed -eq 1 ]; then
    if command -v claude >/dev/null 2>&1; then
      if [ $escalate -eq 1 ]; then
        echo -e "${YELLOW}Running claude -p with --model claude-opus-4-7 (escalation).${NC}" >&2
        result=$(claude -p "$prompt_text" --output-format text --dangerously-skip-permissions --model claude-opus-4-7 2>&1) || true
        agent_used="claude-opus"
      else
        echo -e "${YELLOW}Falling back to claude for this iteration. Next iteration will retry opencode.${NC}" >&2
        result=$(claude -p "$prompt_text" --output-format text --dangerously-skip-permissions 2>&1) || true
        agent_used="claude"
      fi
    else
      echo -e "${RED}Neither opencode nor claude available — cannot proceed.${NC}" >&2
      exit 2
    fi
  else
    agent_used="opencode"
  fi

  echo -e "${BLUE}[agent=$agent_used]${NC}"

  echo "$result"
  echo ""

  # Update escalation counter based on whether this iter flipped a story.
  passes_after=$(grep -c '"passes": true' prd.md 2>/dev/null || echo 0)
  if [ "$passes_after" -gt "$passes_before" ]; then
    echo 0 > "$NO_FLIP_FILE"
  else
    echo $((no_flip_count + 1)) > "$NO_FLIP_FILE"
  fi

  # Check for completion signal
  if [[ "$result" == *"<promise>COMPLETE</promise>"* ]]; then
    echo ""
    echo -e "${GREEN}======================================${NC}"
    echo -e "${GREEN}   ALL TASKS COMPLETE!${NC}"
    echo -e "${GREEN}======================================${NC}"
    echo ""
    echo -e "Finished after ${GREEN}$i${NC} iteration(s)"
    echo ""
    echo "Next steps:"
    echo "  1. Review the completed work in your project"
    echo "  2. Check activity.md for the full build log"
    echo "  3. Review screenshots/ for visual verification"
    echo "  4. Run your tests to verify everything works"
    echo ""
    exit 0
  fi

  echo ""
  echo -e "${YELLOW}--- End of iteration $i ---${NC}"
  echo ""

  # Small delay between iterations to prevent hammering
  sleep 2
done

echo ""
echo -e "${RED}======================================${NC}"
echo -e "${RED}   MAX ITERATIONS REACHED${NC}"
echo -e "${RED}======================================${NC}"
echo ""
echo -e "Reached max iterations (${RED}$MAX_ITERATIONS${NC}) without completion."
echo ""
echo "Options:"
echo "  1. Run again with more iterations: ./ralph.sh 50"
echo "  2. Check activity.md to see current progress"
echo "  3. Check prd.md to see remaining tasks"
echo "  4. Manually complete remaining tasks"
echo ""
exit 1
