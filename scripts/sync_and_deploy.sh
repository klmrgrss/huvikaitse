#!/usr/bin/env bash
# Sünkroonib avaliku EEEL huvikaitse teadmuse Obsidianist (veeb: "avalik") -> Astro -> GitHub Pages.
set -euo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
VAULT_DIR="${EEEL_VAULT_DIR:-/root/projects/eeel-obsidian}"
WEB_DIR="${EEEL_WEB_DIR:-/root/projects/huvikaitse}"

echo "=== [$(date -Iseconds)] EEEL huvikaitse sync start ==="

if [ -d "$VAULT_DIR/.git" ]; then
  git -C "$VAULT_DIR" fetch origin main --quiet || true
  git -C "$VAULT_DIR" pull --rebase --autostash origin main --quiet || true
fi

cd "$WEB_DIR"
python3 scripts/sync_from_obsidian.py

if ! git diff --quiet src/data/knowledge.json 2>/dev/null; then
  npm run build
  git add src/data/knowledge.json
  git commit -m "Auto-sync EEEL teadmusbaasist ($(date +'%Y-%m-%d %H:%M'))"
  if git remote get-url origin >/dev/null 2>&1; then
    GIT_SSH_COMMAND="ssh -i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes" git push origin main
  fi
else
  echo "Avalikus teadmusbaasis muudatusi ei olnud."
fi

