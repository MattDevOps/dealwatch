#!/usr/bin/env bash
# Deploy dealwatch to the VPS so alerts keep coming with the laptop shut.
#
# Same box and key conventions as daytrader (scripts/push_to_vps.sh).
# Ships code + config, installs a systemd --user timer, enables linger.
#
# Usage: ./deploy-vps.sh [--seed]
#   --seed   mark everything currently on the feed as seen (no backlog blast)
set -euo pipefail
cd "$(dirname "$0")"

# Host details live in deploy.env (gitignored), not in this file -- this
# repo is public. Copy deploy.env.example and fill it in.
[ -f deploy.env ] && { set -a; . ./deploy.env; set +a; }

BOX="${DEALWATCH_BOX:-}"
KEY="${DEALWATCH_BOX_KEY:-$HOME/.ssh/id_ed25519}"
KNOWN="${DEALWATCH_BOX_KNOWN:-$HOME/.ssh/known_hosts}"
REMOTE="${DEALWATCH_BOX_PATH:-dealwatch}"

if [ -z "$BOX" ]; then
  echo "set DEALWATCH_BOX (user@host) in deploy.env or the environment" >&2
  exit 2
fi

SSH_OPTS="-i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$KNOWN -o ConnectTimeout=15"
ssh_box() { ssh $SSH_OPTS "$BOX" "$@"; }

echo "Shipping to $BOX:$REMOTE ..."
ssh_box "mkdir -p $REMOTE"
for f in dealwatch.py test_dealwatch.py config.json README.md; do
  rsync -az -e "ssh $SSH_OPTS" "$f" "$BOX:$REMOTE/$f"
done
# Private layer (ntfy topic). Never in git; the box needs it to push alerts.
[ -f config.local.json ] && rsync -az -e "ssh $SSH_OPTS" config.local.json "$BOX:$REMOTE/config.local.json"

echo "Running the test suite on the box ..."
ssh_box "cd $REMOTE && python3 test_dealwatch.py -q 2>&1 | tail -3"

echo "Installing the timer ..."
ssh_box "bash -s" <<REMOTE_EOF
set -euo pipefail
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/dealwatch.service <<UNIT
[Unit]
Description=dealwatch - r/buildapcsales 5090 alerter
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=\$HOME/$REMOTE
ExecStart=/usr/bin/python3 \$HOME/$REMOTE/dealwatch.py --once
UNIT

cat > ~/.config/systemd/user/dealwatch.timer <<'UNIT'
[Unit]
Description=Poll r/buildapcsales for 5090 deals

[Timer]
OnBootSec=2min
OnUnitActiveSec=3min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now dealwatch.timer
loginctl enable-linger \$USER 2>/dev/null || true
systemctl --user list-timers dealwatch.timer --no-pager
REMOTE_EOF

if [ "${1:-}" = "--seed" ]; then
  echo "Seeding remote state ..."
  ssh_box "cd $REMOTE && python3 dealwatch.py --seed | tail -3"
fi

echo
echo "done. useful:"
echo "  ssh \$BOX 'journalctl --user -u dealwatch.service -f'"
echo "  ssh \$BOX 'cd $REMOTE && python3 dealwatch.py --test-alert'"
