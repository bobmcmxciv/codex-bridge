#!/usr/bin/env bash
# Point Claude Code at the cx2cc endpoint on vircs (macOS / Linux).
#
#   source ./client-setup.sh     # this shell only
#   ./client-setup.sh --persist  # also append to ~/.zshrc
#
# Requires the machine to be on the same Tailscale tailnet as vircs.

CX2CC_URL="http://<cx2cc-host>:8901"
CX2CC_KEY="<shared-secret>"

export ANTHROPIC_BASE_URL="$CX2CC_URL"
export ANTHROPIC_API_KEY="$CX2CC_KEY"
# Claude Code sends ANTHROPIC_AUTH_TOKEN as `Authorization: Bearer`, but cx2cc
# only reads `x-api-key`. Unset it so it cannot shadow the key above.
unset ANTHROPIC_AUTH_TOKEN

if [ "$1" = "--persist" ]; then
  RC="${ZDOTDIR:-$HOME}/.zshrc"
  [ -n "$BASH_VERSION" ] && RC="$HOME/.bashrc"
  if grep -q "cx2cc on vircs" "$RC" 2>/dev/null; then
    echo "already present in $RC — not adding again"
  else
    {
      echo ""
      echo "# --- cx2cc on vircs (Codex-backed Claude Code endpoint) ---"
      echo "export ANTHROPIC_BASE_URL=\"$CX2CC_URL\""
      echo "export ANTHROPIC_API_KEY=\"$CX2CC_KEY\""
      echo "unset ANTHROPIC_AUTH_TOKEN"
    } >> "$RC"
    echo "appended to $RC"
  fi
fi

echo "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"
printf 'health: '
curl -s --max-time 10 "$CX2CC_URL/health" || echo "UNREACHABLE - check 'tailscale status'"
echo
printf 'usage:  HTTP '
curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "x-api-key: $CX2CC_KEY" "$CX2CC_URL/usage"
echo " (subscription quota, served from vircs)"
echo "CC Switch quota display: paste cc-switch-usage-script.js (next to this script)"
echo "into 供应商卡片 → 用量查询(📊) → 自定义. {{baseUrl}}/{{apiKey}} come from the provider."
