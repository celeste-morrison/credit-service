#!/usr/bin/env bash
set -euo pipefail

API="${API_URL:-http://localhost:${API_PORT:-8000}}"
USER_ID="u_demo_$RANDOM"
KEY="${CREDITS_API_KEYS%%,*}"
KEY="${KEY:-dev-local-key}"

pretty() { python3 -m json.tool; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
get() { curl -sS -H "X-API-Key: $KEY" "$API$1"; }
post() { curl -sS -X POST "$API/credits" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' "$@"; }

step "POST three credits for $USER_ID (100.00, 50.25, 0.10)"
for amount in 100.00 50.25 0.10; do
  post -d "{\"user_id\": \"$USER_ID\", \"amount\": \"$amount\"}"; echo
done

step "User immediately after POST"
get "/users/$USER_ID" | pretty

step "Polling until status is processed"
for _ in $(seq 1 20); do
  status=$(get "/users/$USER_ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  [[ "$status" == "processed" ]] && break
  sleep 0.5
done
get "/users/$USER_ID" | pretty

step "Retrying with the same Idempotency-Key returns the original task"
post -H 'Idempotency-Key: demo-key-'"$USER_ID" -d "{\"user_id\": \"$USER_ID\", \"amount\": \"5.00\"}"; echo
post -H 'Idempotency-Key: demo-key-'"$USER_ID" -d "{\"user_id\": \"$USER_ID\", \"amount\": \"5.00\"}"; echo

step "Invalid amount is rejected"
post -d "{\"user_id\": \"$USER_ID\", \"amount\": \"-1\"}" | pretty

step "Request without an API key is rejected"
curl -sS -X POST "$API/credits" -H 'Content-Type: application/json' \
  -d "{\"user_id\": \"$USER_ID\", \"amount\": \"1\"}" | pretty
