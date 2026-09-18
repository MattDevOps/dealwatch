#!/usr/bin/env bash
# Deploy both watchers to the VPS so alerts keep coming with the laptop shut.
#
# Runs the test suites here, ships the tracked code + config, runs the
# suites again on the box, and rewrites this repo's block in the box's
# crontab. Cron, not systemd --user timers: the box has Linger=no, so user
# timers would not survive a reboot.
#
# Each <name>.vps.json becomes the box's <name>.host.json (its role).
# dealwatch runs in full. carwatch runs its carwiz source only: yad2 blocks
# datacenter IPs outright.
#
# Usage: ./deploy-vps.sh [--seed]
#   --seed   first deploy only: mark everything on the reddit feed as seen
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

SSH_OPTS="-i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$KNOWN -o ConnectTimeout=15 -o BatchMode=yes"
ssh_box() { ssh $SSH_OPTS "$BOX" "$@"; }
ship() { rsync -az -e "ssh $SSH_OPTS" "$@"; }

# minute-field | watcher | mode. The whole block is rewritten on every
# deploy, so editing a schedule here (or dropping a row) always lands.
JOBS="
*/3  dealwatch --once
7    dealwatch --watchdog
*/10 carwatch  --once
37   carwatch  --watchdog
"

echo "Running the test suites here first (the box is live; do not ship red) ..."
./run-suites.sh

echo "Shipping to $BOX:$REMOTE ..."
ssh_box "mkdir -p $REMOTE"
mapfile -t FILES < <(git ls-files '*.py' '*.json' run-suites.sh README.md)
ship "${FILES[@]}" "$BOX:$REMOTE/"
for role in *.vps.json; do
  ship "$role" "$BOX:$REMOTE/${role%.vps.json}.host.json"
done
# Private layer (ntfy topic). Never in git; the box needs it to push alerts.
[ -f config.local.json ] && ship config.local.json "$BOX:$REMOTE/config.local.json"

echo "Running the test suites on the box (older python there) ..."
ssh_box "$REMOTE/run-suites.sh"

echo "Installing the cron block ..."
ssh_box "JOBS='$JOBS' REMOTE='$REMOTE' bash -s" <<'REMOTE_EOF'
set -euo pipefail
DIR="$HOME/$REMOTE"
BEGIN="# BEGIN dealwatch (managed by deploy-vps.sh)"
END="# END dealwatch"
block="$BEGIN"
while read -r minute name mode; do
  [ -n "$minute" ] || continue
  block+=$'\n'"$minute * * * * cd $DIR && /usr/bin/python3 $name.py $mode >> $DIR/$name.log 2>&1"
done <<<"$JOBS"
block+=$'\n'"$END"
# Drop the old block, and any pre-marker line that runs from this directory.
current="$(crontab -l 2>/dev/null || true)"
kept="$(sed "/^$BEGIN\$/,/^$END\$/d" <<<"$current" | grep -vF "cd $DIR && " || true)"
printf '%s\n%s\n' "$kept" "$block" | crontab -
crontab -l | sed -n "/^$BEGIN\$/,/^$END\$/p"
REMOTE_EOF

if [ "${1:-}" = "--seed" ]; then
  echo "Seeding remote dealwatch state ..."
  ssh_box "cd $REMOTE && python3 dealwatch.py --seed | tail -3"
fi

echo
echo "done. useful:"
echo "  ssh $BOX 'tail -f $REMOTE/dealwatch.log $REMOTE/carwatch.log'"
echo "  ssh $BOX 'cd $REMOTE && python3 carwatch.py --test-alert'"
