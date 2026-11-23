# fk-flight-finder

Scrapes real round-trip fares from Google Flights for India-origin routes and
stores them in MongoDB (`flightklub.flight_fares`) for the Flight Klub website.
Fares come only from Google Flights. Nothing is invented: when Google answers
nothing, the old fares stay.

*Last updated 24 Nov 2026.*

## How it works now

- **8 origins x 53 destinations** (`routes.json`), trip lengths **3, 4, 5, 6, 7 nights**.
- **Two-step search per destination.** Google's calendar/price-graph call is
  gated (error 13), so every price is a per-date request.
  1. Coarse pass: 18 dates at 5 nights. Dates are spread over different weekdays
     within each month (`COARSE_WEEKDAYS`), plus near-term dates (`SCRAPE_NEAR_DAYS`,
     default 4 and 10 days out) so the current month is never empty.
  2. Fine pass: 3, 4, 6 and 7 nights on each month's cheapest coarse date.
- **Pacing:** about 5 s between requests plus jitter (about 5.8 s measured).
- **Price:** Google's cheapest fare less nothing, then `+10%` (`SCRAPE_PRICE_ADJUST`,
  default 0.10). The raw figure is stored as `scrapedPrice`. Fares outside
  INR 1,500 to 400,000 are dropped.
- **Dead routes:** a route is written off after 3 empty dates in a row
  (`DEAD_AFTER`). Written-off routes get a second chance at the end of the city.
- **Empty spells:** Google sometimes answers "no flights" to everything. A canary
  route (`SCRAPE_CANARY`, default Dubai) is tried; while it is empty the scraper
  waits `SCRAPE_WAVE_PAUSE` seconds (default 600), up to `SCRAPE_WAVE_MAX` (9) times,
  before judging a route.
- **Safety:** a circuit breaker stops a pass that is being blocked; a city with
  more than 15% failed routes is not stored; the backend refuses a set smaller
  than 50% of what is stored.
- **Baggage:** Google's payload lists each airline's own baggage-page URL
  (`payload[11]`). Each fare stores `baggage: [{airline, url}]`. Baggage
  allowances themselves are not in Google's data.
- A city is written to the database only when its whole run finishes. Killing a
  pass loses the city in progress; cities already stored are untouched.

Tests: `tests/test_adaptive.py` (one test needs `selectolax`, so it only runs on
the droplets).

## Machines: three droplets

All DigitalOcean, Ubuntu 24.04, 1 vCPU / 1 GB, 2 GB swap, timezone Asia/Kolkata,
code in `/opt/fk-flight-finder`, hourly cron `0 * * * * /opt/fk-flight-finder/run.sh`.
`run.sh` takes a `flock`, so a pass that is still running makes the next hourly
firing exit; when a pass ends, the next starts within the hour. Logs:
`/var/log/fk-flight-finder.log` (rotated weekly).

| Droplet | IP | `SCRAPE_ORIGINS` (priority order) |
|---|---|---|
| 1 | 139.59.61.222 | bangalore, hyderabad, pune |
| 2 | 168.144.81.14 | delhi, chennai, kolkata |
| 3 | 168.144.122.179 | mumbai, ahmedabad |

Each city takes about 5 to 6 hours, so a full pass is about 15 to 18 hours
(droplet 3 about 11). Cities are scraped in the order listed, so put the ones
you want fresh first. The city list lives in each droplet's `.env`
(`SCRAPE_ORIGINS=a,b,c`); a change applies when the next pass starts.

```bash
ssh -i ~/.ssh/trripah_droplet -o IdentitiesOnly=yes root@<ip>
```

`.env` (never in the repo) holds `API_BASE`, `FARE_INGEST_SECRET`, `PROXY_URLS`
(empty: Google serves the droplet IPs directly), `SCRAPE_NIGHTS`, `SCRAPE_ORIGINS`.
To set up a new droplet: copy `droplet_setup.sh`, `requirements.txt`, `run.sh`,
`scrape.py`, `routes.json` to `/opt/fk-flight-finder`, copy `.env` from an existing
droplet over a pipe (do not print it), then `ORIGINS=a,b bash droplet_setup.sh`.

## Deploy

Pushing `scrape.py`, `routes.json` or `run.sh` to `main` runs
`.github/workflows/deploy.yml`, which copies the three files to each droplet and
checks that `scrape.py` parses. It never touches `.env` and does not restart a
running pass: the next pass starts on the new code. The workflow has three jobs:

| Droplet | Needs |
|---|---|
| 1 | secrets `DROPLET_HOST`, `DROPLET_KNOWN_HOSTS`, `DROPLET_USER`, `DROPLET_SSH_KEY` |
| 2 | variable `SECOND_DROPLET=true`; secrets `DROPLET2_HOST`, `DROPLET2_KNOWN_HOSTS` |
| 3 | variable `THIRD_DROPLET=true`; secrets `DROPLET3_HOST`, `DROPLET3_KNOWN_HOSTS` |

`DROPLET_SSH_KEY` and `DROPLET_USER` are shared by all three. Run the workflow by
hand with `restart` ticked to restart passes immediately (the city in progress is
discarded).

## Running by hand

```bash
cd /opt/fk-flight-finder
./.venv/bin/python scrape.py --origins bangalore    # one city
./.venv/bin/python scrape.py --dry-run --limit 1    # about 1 min, writes nothing
```

Restarting a pass: `pkill -f "[s]crape.py"`, remove
`/var/lock/fk-flight-finder.started`, then start `run.sh` in a separate command.
Check progress with `ps -eo etime,cmd | grep "[s]crape.py"` and
`tail /var/log/fk-flight-finder.log`; a stored city logs `-> N fares, stored=True`.

## Backend (the Flight Klub website)

`POST /api/flight-fares` (header `x-fare-secret`) replaces every stored fare for
one origin, drops rows with a bad date or non-positive price, and refuses an
empty set. The website reads Mongo directly (`src/lib/flight-fares/store.ts`).
The route is deliberately not under `/api/admin`, whose auth gate would reject
the scraper before the secret check.

## Rules that stay

- Never show an invented fare; an empty result must never overwrite good data.
- A "month" is the month you depart in; the return may fall in the next one.
- Keep every priced date pair, not just the cheapest.
- "Below average" compares a fare with the same trip length on the same route.

## Why not Travelpayouts or a browser

Travelpayouts' free tier is a cache of other people's searches with no data for
future months; do not re-test it. `fast-flights` builds a protobuf query and
parses the HTML with no browser, which is lighter and harder to detect than
Playwright. `browser_scrape.py` exists only to measure the gap between this
path and what a browser sees (the reason for the 10% adjustment).

## Google Flights response formats

Recorded 18 Sep 2026 from real responses, because these look like blocks and
are not. **Do not re-derive this.**

The data lives in a `<script class="ds:1">` tag as a JSON payload. The library
reaches it via `payload[3][0]`, a list of itineraries. Five distinct shapes have
been observed:

| Shape | What it means | Handling |
|---|---|---|
| `payload[3][0]` is a list, every `k[1][0]` has ≥2 elements | normal, fully priced | parsed |
| One or more `k[1][0] == []` | Google shows the option but will not price it | **skip that entry, keep the rest** |
| `payload[3][0]` is `None` or `[]` | genuinely no flights on the route/date | no fares, not an error |
| payload text ends `errorHasStatus: true` | Google returned an error state | raises `FlightsNotFound` |
| no `script.ds:1` node at all | not a results page — consent wall, CAPTCHA, or a layout change | treat as a hard failure worth investigating |

### The trap, and why it was expensive

`fast_flights.parser.parse_js` does `price = k[1][0][1]` for every itinerary
with no guard. A single unpriced entry raises `IndexError`, which aborts the
whole parse and **throws away every priced itinerary in that response**.

That is not a marginal loss. Measured on the 18 Sep pass:

| Request | Itineraries | Priced | Returned before |
|---|---|---|---|
| BLR→DEL 2026-11-15 | 37 | 36 | nothing |
| BLR→KIX 2026-10-28 | 6 | 5 | nothing |
| BLR→DXB 2026-09-28 | — | 4 | nothing |

31 of 225 requests (13.8%) failed this way, each discarding a full page of
usable fares over one unpriced row.

`_tolerant_parse()` in `scrape.py` re-parses the same payload and skips the
unpriced entries. Verified recovery on all three cases above: 5, 36 and 4
priced itineraries respectively, where the library returned none.

### Payload field indices

Mirrors the library's own parser. If Google changes the shape, both break
together and the error points here.

```
entry[1][0][1]      price
entry[0][1]         airlines
entry[0][2]         segments
  segment[3]        origin IATA        segment[6]   destination IATA
  segment[8]        departure time     segment[10]  arrival time
  segment[20]       departure date     segment[21]  arrival date  [yyyy, mm, dd]
  segment[11]       duration, minutes  segment[17]  aircraft type
entry[0][22][7]     carbon emission    [22][8]      typical for route
payload[7][1][0]    alliances          payload[7][1][1]  airlines lookup
```

### Telling a parse problem from a block

- `IndexError` on a handful of requests, priced results elsewhere in the same
  pass → parser, not a block. Now recovered automatically; the log says
  `~ recovered N priced via tolerant parse`.
- `NO_SCRIPT_ds1`, or every request in a pass failing → investigate. That is
  what an actual block looks like.
- The same ~14% rate appeared from a home connection before the droplet
  existed, which is how we know it is the parser and not the IP.

## Route schedules (`schedule_probe.py`)

Which routes have non-stop flights, on which weekdays, and by which airlines.
For every origin x destination it runs a non-stop one-way search for each day
of one week (schedules repeat weekly) and writes:

- `out/schedules.xlsx` — Routes sheet (direct yes/no, airlines with their days,
  Mon..Sun, days/week, Daily / 5-6x weekly / Alternate days / 1-2x weekly /
  No direct, departure times), a Flights sheet with one row per flight, and an
  About sheet.
- `out/schedules.json` — the same for the scraper.

About 1,330 requests (~2.5h). Progress is saved per route and a re-run
resumes. Best run on a laptop, not the droplet; on the droplet it refuses to
run while a fare pass holds the lock.

```bash
pip install fast-flights openpyxl
python3 schedule_probe.py                   # all routes, week 3+ weeks out
python3 schedule_probe.py --origins pune    # one origin
python3 schedule_probe.py --xlsx-only       # rebuild the sheet from the JSON
```
