#!/usr/bin/env bash
# Install the station Wi-Fi watchdog (issue #686) on this station -- one
# command, run as todd (it asks for the sudo password once):
#
#     bash ~/aviary/Servers/install-netwatch.sh
#
# Safe to re-run: every step overwrites with the repo's copy. It stops at the
# first problem and says what it was. It never disconnects the Wi-Fi, so it
# is safe over ssh.
set -euo pipefail

AVIARY=/home/todd/aviary
ENV_FILE=/home/todd/station.env

step() { printf '\n== %s\n' "$*"; }
die()  { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }

[ "$(id -un)" = todd ] || die "run this as todd, not root (no sudo in front)"

step "1/5  Is the update here?"
for f in flyway/netwatch.py Servers/station-netwatch.service \
         Servers/station-netwatch.timer Servers/station-netwatch.sudoers; do
  [ -f "$AVIARY/$f" ] || die "$AVIARY/$f is missing -- the update hasn't reached this station yet. Try again later."
done
echo "yes"

step "2/5  Dry run (one look at the network; changes nothing)"
# station.env is systemd syntax, never source it -- take just the one line.
url=$(grep -E '^MERLE_FRONTDOOR_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
[ -n "$url" ] || die "MERLE_FRONTDOOR_URL is not set in $ENV_FILE"
out=$(cd "$AVIARY" && MERLE_FRONTDOOR_URL="$url" PYTHONDONTWRITEBYTECODE=1 \
      venv/bin/python -m flyway.netwatch --dry-run) \
  || die "the dry run crashed (output above)"
echo "$out"
grep -q 'iface wl' <<<"$out" || die "no Wi-Fi interface found"
grep -qE 'usb [0-9]' <<<"$out" || die "the Wi-Fi interface isn't a USB device"

step "3/5  Sudo permissions (asks for your password)"
sudo visudo -cf "$AVIARY/Servers/station-netwatch.sudoers" \
  || die "the sudoers file failed its syntax check; nothing installed"
sudo install -m 0440 -o root -g root "$AVIARY/Servers/station-netwatch.sudoers" \
  /etc/sudoers.d/station-netwatch
echo "ok"

step "4/5  The timer"
sudo cp "$AVIARY/Servers/station-netwatch.service" "$AVIARY/Servers/station-netwatch.timer" \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now station-netwatch.timer
sudo systemctl start station-netwatch.service || true  # one real round now; judged below
echo "ok"

step "5/5  Check"
sudo -k   # forget the password, so this proves the watchdog's lines need none
sudo -n -l /usr/bin/systemctl reboot >/dev/null \
  || die "the watchdog's sudo lines aren't working"
systemctl is-active --quiet station-netwatch.timer || die "the timer isn't running"
systemctl list-timers station-netwatch.timer --no-pager | head -2 || true
if ! systemctl is-failed --quiet station-netwatch.service; then
  echo
  echo "DONE. The watchdog runs every 2 minutes and stays silent while the link is fine."
  echo "Its log, if it ever acts:  journalctl -u station-netwatch"
else
  journalctl -u station-netwatch -n 20 --no-pager
  die "the first round failed -- see the log above"
fi
