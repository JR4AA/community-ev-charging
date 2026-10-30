#!/usr/bin/env bash
# Check from outside the your network that the tunnel exposes only what it should.
# Run from any machine:  ./check-public.sh [https://ev.example.org]
set -uo pipefail

base="${1:-https://ev.example.org}"
fail=0

expect() {
    local want=$1 path=$2 got
    got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$base$path")
    if [[ "$got" == "$want" ]]; then
        printf 'ok    %s  %s\n' "$got" "$path"
    else
        printf 'FAIL  %s  %s (want %s)\n' "$got" "$path" "$want"
        fail=1
    fi
}

# Public: whatever the app answers, it must not be the edge 404.
for p in / /pay/health; do
    got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$base$p")
    if [[ "$got" == "404" || "$got" == "000" ]]; then
        printf 'FAIL  %s  %s (should reach the app)\n' "$got" "$p"; fail=1
    else
        printf 'ok    %s  %s\n' "$got" "$p"
    fi
done

# Private: must be refused at the edge.
expect 404 /admin
expect 404 /admin/
expect 404 /api/sessions
expect 404 /ocpp/CP9
expect 404 /ocpp/CP1/x
expect 404 /payx
expect 404 /webhooks/other

# The charger route: a WebSocket handshake with no password must reach the app and get its 401.
# (A missing password doesn't count towards the app's lockout, so this is safe to repeat.)
got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 --http1.1 \
      -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
      -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Protocol: ocpp1.6" \
      "$base/ocpp/CP1")
if [[ "$got" == "401" ]]; then printf 'ok    %s  %s\n' "$got" "/ocpp/CP1 (handshake, no password)"
else printf 'FAIL  %s  %s (want 401 from the app)\n' "$got" "/ocpp/CP1 (handshake, no password)"; fail=1; fi

exit $fail
