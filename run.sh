#!/bin/bash
# Continuous pass. Cron fires hourly and flock serialises, so a pass starts
# again within the hour of the previous one finishing — the board is as fresh
# as the scrape can make it rather than once-a-night fresh.
#
# flock is not optional: a pass that overruns must not have the next hour stack
# on top of it. Two scrapers hitting Google from one IP is exactly the burst
# that earns a CAPTCHA, and the second would also fight the first over the
# replace-per-origin write.
#
# A pass takes ~8h at DELAY=5s over ~3,100 requests, so most hourly firings are
# expected to find the lock held and exit. That is the normal state now, not an
# anomaly, so it is not logged — one line an hour would bury the real output.
# What IS worth saying is a pass that has run far longer than one ever should,
# which means hung rather than busy.
LOG=/var/log/fk-flight-finder.log
STARTED=/var/lock/fk-flight-finder.started
STUCK_AFTER=$((20 * 3600))   # a healthy pass is ~8h; 20h means something hung

exec 9>/var/lock/fk-flight-finder.lock
if ! flock -n 9; then
  if [ -f "$STARTED" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$STARTED") ))
    if [ "$age" -gt "$STUCK_AFTER" ]; then
      echo "$(date -Iseconds) WARNING: pass still running after $((age / 3600))h — likely hung" >> "$LOG"
    fi
  fi
  exit 0
fi

date +%s > "$STARTED"
cd /opt/fk-flight-finder
echo "=== $(date -Iseconds) start ===" >> "$LOG"
./.venv/bin/python scrape.py >> "$LOG" 2>&1
echo "=== $(date -Iseconds) exit=$? ===" >> "$LOG"
