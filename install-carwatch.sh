#!/usr/bin/env bash
# Install carwatch as a systemd --user timer (polls every 10 minutes).
#
# yad2 sits behind a bot wall that only a real, windowed Chrome gets past,
# so the poll runs google-chrome under Xvfb (a virtual display: no window
# ever shows). Needs: google-chrome, xvfb-run (dnf install
# xorg-x11-server-Xvfb), python3 with venv. Everything else is installed
# here into ./.venv.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
INTERVAL="${CARWATCH_INTERVAL:-10min}"

for tool in google-chrome xvfb-run python3; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

if [ ! -x "$DIR/.venv/bin/python" ]; then
  python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install -q --upgrade patchright
PY="$DIR/.venv/bin/python"

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/carwatch.service" <<UNIT
[Unit]
Description=carwatch - facelift Ioniq 5 listing alerter (yad2 + carwiz)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=/usr/bin/xvfb-run -a $PY $DIR/carwatch.py --once
TimeoutStartSec=8min
UNIT

cat > "$UNIT_DIR/carwatch.timer" <<UNIT
[Unit]
Description=Poll yad2 + carwiz for a facelift Ioniq 5

[Timer]
OnBootSec=3min
OnUnitActiveSec=$INTERVAL
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

cat > "$UNIT_DIR/carwatch-watchdog.service" <<UNIT
[Unit]
Description=carwatch watchdog - warn if polling stops

[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=$PY $DIR/carwatch.py --watchdog
UNIT

cat > "$UNIT_DIR/carwatch-watchdog.timer" <<'UNIT'
[Unit]
Description=Check that carwatch is still polling

[Timer]
OnBootSec=15min
OnUnitActiveSec=1h
AccuracySec=5min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now carwatch.timer carwatch-watchdog.timer
loginctl enable-linger "$USER" 2>/dev/null || \
  echo "note: run 'sudo loginctl enable-linger $USER' to keep polling when logged out"

echo
echo "installed. status:"
systemctl --user list-timers 'carwatch*' --no-pager
echo
echo "first poll:  systemctl --user start carwatch.service   (sends the 'armed' digest)"
echo "logs:        journalctl --user -u carwatch.service -f"
echo "stop:        systemctl --user disable --now carwatch.timer carwatch-watchdog.timer"
