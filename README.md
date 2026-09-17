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

### Storage — the Trripah CRM

`https://trripah-crm-nextjs.vercel.app`, MongoDB database `trripah_crm`.

| Endpoint | Auth | Purpose |
|---|---|---|
| `/api/flight-deals` | `x-flight-secret` on write; open read | the board/fare store |
| `/api/flight-search-cache` | `x-flight-secret` on both | per-search cache |
| `/api/flight-quota` | `x-flight-secret` on both | metered-provider budget |

POST shape: `{ originSlug, deals[] }`. It **replaces** all rows for that origin
and **refuses an empty array**, so a failed scrape leaves the previous data
alone rather than wiping the board. Read limit is 400 rows per origin.

**Two blockers before anything can be stored:**

1. Commit `f987fec` in `trripah_crm_nextjs` must be pushed and deployed.
   Verify with: `curl -s -o /dev/null -w '%{http_code}'
   'https://trripah-crm-nextjs.vercel.app/api/flight-deals?origin=bangalore'`
   — must be **200**, not 401.
2. **`FLIGHT_FINDER_SECRET` does not match.** The `trripah_website` `.env`
   value is 57 characters, the `trripah_crm_nextjs` one is 58. Neither local
   file is authoritative — **the CRM's Vercel environment variable is.** Read it
   there and make the scraper send exactly that.

---

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
            POST {CRM}/api/flight-deals  (x-flight-secret)
                                │
                                ▼
                    website reads and renders
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
4. Push and deploy `f987fec`; confirm `/api/flight-deals` returns 200.
5. Read `FLIGHT_FINDER_SECRET` from the CRM's Vercel env; align the scraper.
6. Port the scraper from `trripah_website@761de79^:scripts/flight-scraper/`,
   keeping **every** priced date pair.
7. Dry-run one origin, confirm November and December fares appear, then store.
8. Add cron at 03:30 IST, well clear of any site cron.
