#!/usr/bin/env bash
set -euo pipefail

if ! command -v tailscale >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Tailscale CLI is not installed.
Install it first (Ubuntu/WSL):
  curl -fsSL https://tailscale.com/install.sh | sh
Then authenticate this WSL machine:
  sudo tailscale up
EOF
  exit 1
fi

if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale is not connected. Run: sudo tailscale up" >&2
  exit 1
fi

# Serve is private to the tailnet. This intentionally does not use `tailscale funnel`.
tailscale serve --https=443 http://127.0.0.1:8080
echo
echo "Published privately within your tailnet. Open the HTTPS URL shown above on a Tailscale-connected phone."
echo "To inspect or remove the rule: tailscale serve status / tailscale serve reset"
