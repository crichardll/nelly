#!/usr/bin/env bash
# deploy.sh — push local changes to GitHub, then make EC2 pull and restart.
#
# Why this exists:
#   GitHub is the source of truth. Production is just "whatever main is on GitHub."
#   Run this after committing changes locally.

set -euo pipefail

EC2_HOST="ubuntu@100.30.215.66"
SSH_KEY="$HOME/.ssh/nelly-bot.pem"

# 1. Refuse to deploy if there are uncommitted changes — production must match a commit.
if ! git diff-index --quiet HEAD --; then
  echo "❌ Uncommitted changes. Commit them first, then re-run." >&2
  exit 1
fi

# 2. Push to GitHub.
echo "→ git push"
git push

# 3. SSH in, pull, restart, and show the latest log lines so you can confirm
#    the bot came back up cleanly.
echo "→ ssh ec2 && git pull && systemctl restart"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o LogLevel=ERROR "$EC2_HOST" bash -s <<'REMOTE'
set -e
cd ~/nelly
git pull --ff-only
.venv/bin/pip install --quiet -r requirements.txt   # pick up new deps if any
sudo systemctl restart nelly
sleep 2
sudo systemctl is-active nelly
echo "--- last 6 log lines ---"
sudo journalctl -u nelly -n 6 --no-pager
REMOTE

echo "✅ Deployed."
