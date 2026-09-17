#!/bin/bash
# Nightly pass. Scheduled 02:00 IST so a ~3 hour run finishes around 05:00,
# well inside the 02:00-07:00 window and hours clear of the 07:00 deadline.
#
# flock is not optional: a pass that overruns must not have the next day stack
# on top of it. Two scrapers hitting Google from one IP is exactly the burst
# that earns a CAPTCHA, and the second would also fight the first over the
# replace-per-origin write.
LOG=/var/log/fk-flight-finder.log
exec 9>/var/lock/fk-flight-finder.lock
if ! flock -n 9; then
  echo "$(date -Iseconds) skipped: previous pass still running" >> "$LOG"
  exit 0
fi
cd /opt/fk-flight-finder
echo "=== $(date -Iseconds) start ===" >> "$LOG"
./.venv/bin/python scrape.py >> "$LOG" 2>&1
echo "=== $(date -Iseconds) exit=$? ===" >> "$LOG"
