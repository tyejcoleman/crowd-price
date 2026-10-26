#!/usr/bin/env bash
# save-credential.sh: move a credential from the macOS clipboard into .env without ever printing it.
# Usage:  harness/save-credential.sh id       (PayPal sandbox Client ID)
#         harness/save-credential.sh secret   (PayPal sandbox Client Secret)
# Flow: copy the value in the PayPal dashboard, run this, it saves to .env (mode 600) and clears the clipboard.
set -eu
cd "$(git rev-parse --show-toplevel)"

case "${1:-}" in
  id)     KEY=PAYPAL_CLIENT_ID;     OTHER=PAYPAL_CLIENT_SECRET ;;
  secret) KEY=PAYPAL_CLIENT_SECRET; OTHER=PAYPAL_CLIENT_ID ;;
  *) echo "usage: $0 id|secret" >&2; exit 2 ;;
esac

[ -f .env ] || { cp .env.example .env; }
chmod 600 .env

val="$(pbpaste | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

# Sanity checks (never print the value).
[ -n "$val" ] || { echo "clipboard is empty: copy the value first" >&2; exit 1; }
case "$val" in *[[:space:]]*) echo "clipboard has spaces: that is not a credential" >&2; exit 1 ;; esac
[ "${#val}" -ge 20 ] || { echo "clipboard value is too short (${#val} chars)" >&2; exit 1; }
[ "${#val}" -le 200 ] || { echo "clipboard value is too long (${#val} chars)" >&2; exit 1; }
case "$val" in *[!A-Za-z0-9_-]*) echo "clipboard has unexpected characters: not a PayPal credential" >&2; exit 1 ;; esac

# Refuse to save the same value under both names (a common slip).
other_val="$(grep -E "^${OTHER}=" .env | head -1 | cut -d= -f2- || true)"
if [ -n "$other_val" ] && [ "$other_val" = "$val" ]; then
  echo "this is the same value already saved as ${OTHER}: copy the other one" >&2; exit 1
fi

# Replace or append, using a temp file so the value never appears in a process list.
tmp="$(mktemp)"; chmod 600 "$tmp"
if grep -qE "^${KEY}=" .env; then
  awk -v k="$KEY" -v v="$val" 'BEGIN{FS=OFS="="} $1==k {print k"="v; next} {print}' .env > "$tmp"
else
  { cat .env; printf '%s=%s\n' "$KEY" "$val"; } > "$tmp"
fi
mv "$tmp" .env; chmod 600 .env

printf '' | pbcopy   # clear the clipboard
echo "saved ${KEY} (${#val} chars) to .env, clipboard cleared"
