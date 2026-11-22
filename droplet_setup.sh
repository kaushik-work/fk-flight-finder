#!/bin/bash
# Bootstrap a SECOND scraper droplet (Ubuntu). Run once on its console; safe to run twice.
#
#   ORIGINS=chennai,kolkata,pune,ahmedabad bash droplet_setup.sh
#
# It installs Python and the scraper's one dependency, adds swap, sets the hourly cron that run.sh expects, and
# writes SCRAPE_ORIGINS so this droplet scrapes only its half of the cities. The scraper code itself arrives from
# GitHub Actions on the next push (see .github/workflows/deploy.yml). .env needs API_BASE and FARE_INGEST_SECRET
# copied from the first droplet by hand: secrets never live in the repo.
set -euo pipefail
ORIGINS="${ORIGINS:?set ORIGINS, for example ORIGINS=chennai,kolkata,pune,ahmedabad}"

timedatectl set-timezone Asia/Kolkata
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
apt-get update -y
apt-get install -y python3-venv util-linux cron

mkdir -p /opt/fk-flight-finder
cd /opt/fk-flight-finder
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q -r requirements.txt   # the exact versions the first droplet runs

touch .env && chmod 600 .env
if grep -q '^SCRAPE_ORIGINS=' .env; then
  sed -i "s/^SCRAPE_ORIGINS=.*/SCRAPE_ORIGINS=$ORIGINS/" .env
else
  echo "SCRAPE_ORIGINS=$ORIGINS" >> .env
fi

( crontab -l 2>/dev/null | grep -v 'fk-flight-finder/run.sh' || true; echo '0 * * * * /opt/fk-flight-finder/run.sh' ) | crontab -
cat > /etc/logrotate.d/fk-flight-finder <<'LOGROTATE'
/var/log/fk-flight-finder.log { weekly rotate 14 compress missingok notifempty }
LOGROTATE

echo
echo "Done. Cities on this droplet: $ORIGINS"
echo "Next: put API_BASE and FARE_INGEST_SECRET in /opt/fk-flight-finder/.env, then push to GitHub so the code is copied here."
