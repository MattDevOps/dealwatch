#!/usr/bin/env bash
# Install dealwatch as a systemd --user timer (polls every 3 minutes).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/dealwatch.service" <<UNIT
[Unit]
Description=dealwatch - r/buildapcsales 5090 alerter
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/dealwatch.py --once
UNIT

cat > "$UNIT_DIR/dealwatch.timer" <<'UNIT'
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

# notify-send from a user unit needs the session bus/display in the env.
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now dealwatch.timer

# Alerts keep firing while logged out only if lingering is on.
loginctl enable-linger "$USER" 2>/dev/null || \
  echo "note: run 'sudo loginctl enable-linger $USER' to keep polling when logged out"

echo
echo "installed. status:"
systemctl --user list-timers dealwatch.timer --no-pager
echo
echo "logs:      journalctl --user -u dealwatch.service -f"
echo "stop:      systemctl --user disable --now dealwatch.timer"
