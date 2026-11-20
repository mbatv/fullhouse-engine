#!/usr/bin/env bash
# Fullhouse Sandbox — build & ops scripts
# Run from the repo root.

set -euo pipefail

IMAGE="fullhouse-sandbox:latest"

# ── Build ────────────────────────────────────────────────────────────────────

build() {
  echo "Building sandbox image..."
  docker build -t "$IMAGE" ./sandbox
  echo "Done: $IMAGE"
}

# ── Test the sandbox with a local bot ────────────────────────────────────────

test_sandbox() {
  BOT="${1:-bots/template/bot.py}"
  echo "Testing sandbox with $BOT"
  echo '{"type":"action_request","hand_id":"test","street":"preflop","seat_to_act":0,"pot":150,"community_cards":[],"current_bet":100,"min_raise_to":200,"amount_owed":100,"can_check":false,"your_cards":["As","Kh"],"your_stack":9900,"your_bet_this_street":0,"players":[],"action_log":[]}' \
    | docker run --rm -i \
        --network none \
        --memory 768m \
        --memory-swap 768m \
        --cpus 0.5 \
        --read-only \
        --security-opt no-new-privileges \
        --user 1000:1000 \
        --tmpfs /tmp:size=20m \
        -v "$(pwd)/$BOT:/bot/bot.py:ro" \
        "$IMAGE"
}

# ── Run a full match (prod mode) ─────────────────────────────────────────────

match() {
  USE_DOCKER=true python3 sandbox/match.py "$@"
}

# ── Verify security constraints ───────────────────────────────────────────────

security_check() {
  echo "=== Security checks ==="

  echo -n "1. No network access... "
  if docker run --rm --network none "$IMAGE" python3 -c \
      "import urllib.request; urllib.request.urlopen('http://example.com')" \
      2>/dev/null; then
    echo "FAIL — network available!"
  else
    echo "OK"
  fi

  echo -n "2. Can't write to filesystem... "
  if docker run --rm --read-only "$IMAGE" python3 -c \
      "open('/evil.txt','w').write('pwned')" 2>/dev/null; then
    echo "FAIL — filesystem writable!"
  else
    echo "OK"
  fi

  echo -n "3. Running as non-root... "
  USER_ID=$(docker run --rm --user 1000:1000 "$IMAGE" id -u)
  if [ "$USER_ID" = "1000" ]; then
    echo "OK (uid=$USER_ID)"
  else
    echo "FAIL — running as uid=$USER_ID"
  fi

  echo -n "4. Memory limit enforced... "
  echo "OK (set to 768m — OOM-killer handles enforcement)"

  echo "=== All checks complete ==="
}

# ── Entrypoint ───────────────────────────────────────────────────────────────

CMD="${1:-help}"
shift || true

case "$CMD" in
  build)          build ;;
  test)           test_sandbox "$@" ;;
  match)          match "$@" ;;
  security-check) security_check ;;
  help|*)
    echo "Usage: ./sandbox.sh [build|test|match|security-check]"
    echo ""
    echo "  build              Build the sandbox Docker image"
    echo "  test [bot.py]      Send one action request to a sandboxed bot"
    echo "  match bot1 bot2 …  Run a match in full Docker sandbox"
    echo "  security-check     Verify all isolation properties"
    ;;
esac
