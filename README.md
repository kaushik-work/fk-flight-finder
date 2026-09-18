# fk-flight-finder

A fare-data service. It scrapes real round-trip prices for a fixed set of
India-origin routes, stores them, and serves them to a website.

**Status: nothing built yet.** This README is the brief. Written 18 Sep 2026,
immediately after the previous attempt was deleted.

---

## Why the last one was deleted

The old Flight Finder lived in `trripah_website` and was removed in commit
`761de79` (8,446 lines, 43 files). Recover any of it with
`git show 761de79^:<path>` in that repo — the Python scraper and the ranking
logic are both worth reading before rewriting them.

The UI, ranking, filtering and intent parsing were fine. **The data source was
the whole problem**, and it failed in three stacked layers. Each one hid the
next, so fixing one changed nothing visible.

### Layer 1 — Travelpayouts' free tier has no data for future months

It is a cache of what other people recently searched, not a fare feed. Measured
17 Sep 2026 for BLR→DXB:

| Endpoint | Months returned |
|---|---|
| `/v1/prices/calendar` | Oct 2026, Jan 2027 only |
| `/aviasales/v3/prices_for_dates` | zero rows for Nov and Dec |
| `/v2/prices/latest` (`period_type=year`, 100 rows) | Sep, Oct, Jan only |

`/v1/prices/calendar` **ignores the month you ask for** and returns the same
rows regardless. The old code filtered them to the requested month, which was
correct, so November rendered empty.

Do not spend time re-testing this. It is not a bug and it is not fixable from
the API side.

### Layer 2 — the scraper could never store anything

A Google Flights scraper was written to fill that gap. It never worked, because
the CRM's auth proxy rejected `/api/flight-deals` with a 401 before the route's
own `x-flight-secret` check ran. Same for `/api/flight-search-cache` and
`/api/flight-quota`. Every caller was blocked: the scraper, the website's board
reads, and the nightly cron.

The `flight_deals` collection **does not exist** in the database, because
nothing was ever able to create it.

Fixed in `trripah_crm_nextjs` commit `f987fec` — **still unpushed as of writing.**

### Layer 3 — the scraper threw away 89% of what it fetched

It priced nine date pairs per route, then kept only the cheapest and discarded
the other eight. So even a successful scrape could not answer "Dubai in
November". Fixed in `trripah_website` commit `3e1b039`, which is now deleted
along with the rest — but the lesson stands: **keep every priced date pair.**

---

## The data is there. Google has it.

Same route, same day, via the `fast-flights` Python library:

| Departure | Return | Fare |
|---|---|---|
| 15 Nov 2026 | 20 Nov | ₹31,666 |
| 28 Nov 2026 | 3 Dec | ₹28,254 |
| 15 Dec 2026 | 20 Dec | ₹34,919 |

---

## Machines we have

### Scraper host — DigitalOcean Droplet (new, for this project)

| | |
|---|---|
| Plan | Basic / Regular SSD, **$6/mo** |
| Specs | 1 vCPU, **1 GB RAM**, 25 GB SSD, 1000 GB transfer |
| OS | Ubuntu 24.04 LTS x64 |
| Region | **BLR1, Bangalore** |
| Public IPv4 | enabled — static, ours, whitelistable |
| Monitoring | enabled |
| Backups | off (correct — the box holds no state worth keeping) |
| IP address | **139.59.61.222** (DigitalOcean AP, verified by whois) |
| Hostname | `fk-flight-scraper` |
| Timezone | Asia/Kolkata |
| Swap | 2 GB, added and in `/etc/fstab` |
| Scraper root | `/opt/fk-flight-finder` (venv + `fast-flights` 3.1.0) |

SSH in with:

```bash
ssh -i ~/.ssh/trripah_droplet root@139.59.61.222
```

Key is ed25519, no passphrase, fingerprint
`SHA256:od13NN7FLmYX2mII8439359r/f2ZXbyI+TCN56A/SXs`. Private key stays on the
Mac at `~/.ssh/trripah_droplet`.

Provisioned 18 Sep 2026: swap, hostname, IST timezone, Python 3.12.3, venv at
`/opt/fk-flight-finder/.venv` with `fast-flights` 3.1.0. Nothing else installed
yet — no Chromium, because the protobuf path does not need it.

### Verified: Google serves this IP unproxied

Run from the droplet, 18 Sep 2026, BLR→DXB:

| Departure | Fares returned | Cheapest |
|---|---|---|
| 15 Nov 2026 | 4 | ₹31,714 |
| 15 Dec 2026 | 3 | ₹34,967 |

Within a few hundred rupees of the same queries from a home connection
(₹31,666 / ₹34,919), which is ordinary fare movement. **So the droplet's own IP
is not blocked and returns correct India-localised prices. Proxies are not
needed to begin.** Revisit only if CAPTCHAs appear at full nightly volume.

Bangalore is the right region: Google serves India-localised fares, so an
Indian IP with `curr=INR` is more consistent than scraping from the US or EU.

### Moonlight — the AI agent (do NOT host the scraper here)

DO App Platform app `stingray-app`, component `trripah-origin-moonlight`,
`https://stingray-app-jt3mx.ondigitalocean.app`, BLR1, **$5/mo**.
Java 21 / Spring Boot 3.4.3, MongoDB, Claude Haiku. Repo:
`trripah-origin-moonlight`.

Two hard reasons it cannot host the scraper:

1. **RAM.** It already sits at 53% of 512 MB (~270 MB for the JVM). Chromium
   needs 300–500 MB on its own. Adding it would OOM the agent that handles
   live chat and lead capture.
2. **No usable outbound IP.** Its "Public static ingress IPs"
   (`162.159.140.98`, `172.66.0.96`) are **Cloudflare**, verified by whois.
   They are inbound only. Outbound traffic leaves via DigitalOcean's shared
   egress pool, which is neither static nor ours — so it cannot be whitelisted
   with a proxy provider and builds no reputation.

**Where Moonlight does belong:** deciding *what* to scrape. It sees every chat
and knows what people actually ask for. Let it rank tomorrow's route list by
real demand so we scrape ~400 routes people want instead of 1,800 by rote.
That is a 78% cut in exposure and better data at the same time.

### Backend — the FlightKlub site (NOT the Trripah CRM)

`flight-klub-website`, MongoDB database `flightklub`, collection
`flight_fares`. Route: `app/api/flight-fares/route.ts`.

| Method | Auth | Purpose |
|---|---|---|
| `POST /api/flight-fares` | `x-fare-secret` header | scraper ingest |
| `GET /api/flight-fares` | open | what the frontend reads |

POST body is `{ originSlug, fares[] }` and **replaces** every stored fare for
that origin, so a route whose fare has vanished disappears rather than lingering
at last night's price. It drops rows with a bad date or a non-positive price,
and **refuses an empty array** — a blocked scrape must look like stale prices,
never like there are no flights.

GET filters on `origin`, `destination` and `month`, sorted cheapest first,
capped at 500 rows, cached 30 minutes at the edge. `month` matches the month you
*depart* in.

**Deliberately not under `/api/admin`.** That prefix is gated by `proxy.ts` on
an admin session cookie, and a scraper has no cookie. This is the exact trap
that killed the previous attempt in the CRM repo: the auth gate rejected the
ingest call before the route's own secret check ran, so nothing was ever stored
and the cause stayed invisible. FlightKlub's `proxy.ts` matcher is
`["/admin/:path*", "/api/admin/:path*"]` only — verified — so `/api/flight-fares`
is not gated.

`FARE_INGEST_SECRET` is generated and in `flight-klub-website/.env.local`.
**It must be copied into Vercel for that project**, and into the droplet's
`/opt/fk-flight-finder/.env`.

#### Verified end to end, 18 Sep 2026

Run against a local FlightKlub build with the real database:

| Test | Result |
|---|---|
| POST without the secret | 401 |
| POST with secret, empty `fares[]` | 400, stored rows untouched |
| POST 3 fares, one with a bad date and zero price | `stored: 2, skipped: 1` |
| `GET ?origin=bangalore&month=2026-11` | returned the November fare |
| `GET ?origin=bangalore` | 2 fares across Nov and Dec |

Test rows were deleted afterwards; the collection is empty.

### Frontend — the Trripah website

`trripah_website` renders the fares. Nothing is built there yet — the old
feature was deleted in `761de79`. It should read `GET /api/flight-fares` from
FlightKlub and must keep the display rules below.

## Avoiding blocks — what actually matters

In order of impact. Most published advice gets this ordering wrong.

1. **IP reputation — roughly 80% of the outcome.** Google ranks sources mobile
   > residential > ISP > datacenter, and blocks AWS/GCP/DO ranges hardest.
   Rotating residential proxies are the only real fix. For 1,800 nightly
   requests that is ~2–4 GB/month, about **$5–15**.
2. **Fewer requests.** The cheapest defence is not asking. See the Moonlight
   demand-ranking note above.
3. **Pacing.** Slow and steady from one stable IP beats bursts across rotating
   flagged IPs. Randomise delays — never a fixed 5 seconds. Vary the hour.
4. **Fingerprint, only if a browser is involved.** Real Chrome over Chromium,
   stealth patches for the automation signals, and above all internal
   consistency. A Windows user-agent on a Linux host with Linux fonts and
   timezone is an instant flag.
5. **Session continuity.** A fresh cookieless session per request looks like a
   bot. Persist cookies and consent state per IP.

### Do not switch to a headless browser as the primary path

`fast-flights` **drives no browser at all** — verified: no playwright,
selenium or webdriver anywhere in the package. It builds a protobuf query
(`fast_flights/pb/flights_pb2.py`) and parses the HTML response. That is both
lighter and *harder to detect* than Playwright, which exposes dozens of
fingerprint signals the protobuf path simply does not have.

Use the protobuf path as primary. Add Playwright only as a fallback for when it
fails, so the memory and detection cost is paid rarely rather than always.

Expect occasional blocks regardless. "Fool proof" is achievable only in this
sense: **when a scrape fails, serve cached fares with an honest label — never a
blank page, never an invented number.**

---

## Architecture to build

```
Moonlight ──ranks demand──> route list
                                │
Droplet (BLR1, cron nightly)    ▼
  protobuf fetch (primary) ──┐
  Playwright fetch (fallback)─┴──> every priced date pair
                                │
                                ▼
   POST {flightklub}/api/flight-fares  (x-fare-secret)
                                │
                                ▼
      Trripah website reads GET /api/flight-fares
```

Rules carried over from the old spec, all of which were right:

- **Never show an invented fare.** The old fixture provider refused to run in
  production for exactly this reason. Keep that refusal.
- **Quote slightly high, not low.** Scraped fares read consistently below live
  Google prices, so a display buffer was added per person. A traveller clicking
  through to a *higher* number than advertised has been misled.
- **A month means the month you depart in.** The return may fall in the next
  month, and a 28 Sep → 3 Oct trip is an ordinary September holiday — often the
  cheaper one. Sample late-month, not just mid-month.
- **Cap trip length in one place** and have the scraper, the search and the
  board all read it. A scraped 7-night card that no search can return is a dead
  end. The old cap was 5 nights.
- **An empty result set must never overwrite a good one.**

### Proxy layer

Build it pluggable from day one: runs on the droplet's own IP with nothing
configured, and picks up rotating residential proxies the moment a `PROXY_URLS`
environment variable is set. No code change to switch it on.

---

## Running it

Scheduled on the droplet, 02:00 IST daily:

```
0 2 * * * /opt/fk-flight-finder/run.sh
```

A full pass is ~3 hours, so it finishes near 05:00 — inside the 02:00–07:00
window and hours clear of the 07:00 deadline. `run.sh` takes a `flock` first: a
pass that overruns must not have the next day stack on top of it, because two
scrapers hitting Google from one IP is the burst that earns a CAPTCHA, and the
second would also fight the first over the replace-per-origin write. A skipped
run is logged rather than silent.

Logs go to `/var/log/fk-flight-finder.log`, rotated weekly, 14 kept.

Manual runs:

```bash
ssh -i ~/.ssh/trripah_droplet root@139.59.61.222
cd /opt/fk-flight-finder
./.venv/bin/python scrape.py --origins bangalore       # one origin, ~19 min
./.venv/bin/python scrape.py --dry-run --limit 1       # ~1 min, writes nothing
./.venv/bin/python scrape.py                           # everything, ~3 h
```

Deploy changes from this repo with `./deploy.sh`. It syncs `scrape.py`,
`routes.json` and `run.sh` only — `.env` holds the ingest secret and is managed
on the droplet directly.

### Reading the log

`IndexError` lines are normal, not blocks. Google occasionally serves a layout
the parser does not recognise; the rate is around 7% and the scraper retries
once then moves on rather than hammering a failing route. A real block looks
different: every request failing, or a CAPTCHA page in place of results. The
same rate was seen from a home connection, so it is the parser, not the IP.

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

## Volume

8 origins × 25 destinations × 9 date pairs = **1,800 requests per full pass**,
about 2.5 hours at one every 5 seconds. Not 10,000/day — do not architect for a
scale this does not need.

---

## Open questions

1. **Proxy budget** — resolved for now: the droplet IP works unproxied
   (evidence above), so start free. The fetch layer still reads `PROXY_URLS` so
   residential rotation can be switched on without a code change if blocks
   appear at full volume.
2. **Which site consumes this?** The folder is named `fk-`, but
   `flight-klub-website` is a charter and helicopter business, not a fare search
   product, and the storage contract above lives in the Trripah CRM. Confirm
   whether this serves Trripah, FlightKlub, or both.

## First steps

1. ~~Record the droplet IP.~~ Done.
2. ~~Provision the droplet.~~ Done — swap, Python, venv, `fast-flights`.
3. ~~Confirm Google serves the droplet IP.~~ Done, see above.
4. ~~Port the scraper.~~ Done — `scrape.py` here, deployed to
   `/opt/fk-flight-finder/` on the droplet, keeping every priced date pair.
   A dry run returned 7 Dubai fares across Oct, Nov, Dec and Jan.
5. ~~Build the backend.~~ Done — `flight-klub-website`
   `app/api/flight-fares/route.ts`, verified end to end.
6. Copy `FARE_INGEST_SECRET` from `flight-klub-website/.env.local` into that
   project's Vercel env, and into `/opt/fk-flight-finder/.env` on the droplet.
7. Deploy FlightKlub, then run `scrape.py --origins bangalore` for real and
   confirm rows land in `flight_fares`.
8. Add cron at 03:30 IST.
9. Build the frontend in `trripah_website` against `GET /api/flight-fares`.
