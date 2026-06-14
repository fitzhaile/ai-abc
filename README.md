# America's Boating Club — Ecosystem Dashboard

A static dashboard built from a thorough scrape of https://americasboatingclub.org
and every related America's Boating Club property linked from it (the Ship's
Store, the EnrolMart course store, the video channel, the donation site, the
BoatLive 365 campaign site, the member portal's public pages, and two
ABC-branded vendor storefronts), plus data captured by **interacting** with the
sites: the find-a-club locator's GeoJSON search, each club's upcoming-class
schedule, and the storefront catalogs.

**To view the dashboard:** open `dashboard/index.html` in a browser
(or `cd dashboard && python3 -m http.server` and visit http://localhost:8000).

## The one rule

**The dashboard never contains a hand-typed number.** Every figure is computed
by `derive/build_dashboard_data.py` from files on disk, every record traces to
a saved raw HTML page or captured API response, and the dashboard's
"Sources & Methods" section names the script and source behind each dataset.
If a value cannot be traced through that chain, it is a defect.

The deliberately editorial surfaces are three tabs, each labeled as such in the
UI:

- **Opportunities** — drafted recommendations. Figures in each card are either
  computed from `data.js` at render time (tagged "computed from crawl") or
  quoted from the audit below and verified against our own crawl (tagged "audit
  finding · verified").
- **Problems** and **Funnels** — these reproduce an *external web-estate audit*
  (page-sourced 11 Jun 2026, AI-assisted, carrying its own "unverified" caveat).
  They are findings and judgment, **not our measured data**, and say so at the
  top of each tab. Where our crawl confirms or corrects a finding, an inline
  note marks it — including one correction: the audit could not confirm flagship
  analytics, but our crawl found GA4 on it (shown in the Web Properties matrix).

The **Web Properties** analytics matrix and tech-platform table, by contrast, *are*
measured data: `derive/detect_tech.py` reads every property's saved page source
and reports only IDs/platforms it can point to, with the evidence; a property it
could not crawl is labeled "not crawled" rather than left blank. The channel's
category counts on the **Channel** tab were captured live from the Uscreen
catalog into `data/interactive/channel_categories.json` (a browser capture, not
a `scrape/` script) and spot-verified against the per-category pages.

## Pipeline (each stage independently runnable)

```
1. CRAWL          python3 scrape/crawl.py
                  → data/crawl/index.jsonl  (one plain-English record per URL)
                  → data/raw/<domain>/*.html
                  → data/crawl/crawl.log    (human-readable decisions log)

2. INTERACTIVE    python3 scrape/fetch_club_locations.py    (club directory via
                  CAPTURES         the find-a-club GeoJSON search, probed radius)
                  python3 scrape/fetch_club_schedules.py    (255 club-details
                                   pages → national class schedule; cached)
                  python3 scrape/fetch_store_catalog.py     (Ship's Store walk,
                                   honors its 20s robots.txt Crawl-Delay;
                                   --reparse rebuilds from saved HTML)
                  python3 scrape/fetch_satellite_stores.py  (Geiger closure
                                   notice + C.P. Dean Shopify products.json)
                  → data/interactive/*.json (+ raw responses/HTML alongside)

3. EXTRACT        python3 derive/extract.py        (offline: reads only disk)
                  → data/extracted/*.json  (pages, education catalog, EnrolMart
                    products, videos, link graph, sitemap lastmod dates)

3b. TECH SWEEP    python3 derive/detect_tech.py    (offline: reads data/raw/)
                  → data/extracted/tech_inventory.json  (per-property analytics
                    tags + tech platform + social handles, with the page-source
                    evidence for each — powers the Web Properties matrix/table)

4. DERIVE         python3 derive/build_dashboard_data.py    (offline)
                  → dashboard/data.js + dashboard/data.json
```

Re-running stages 3–4 is deterministic: same inputs → byte-identical outputs
(verified). Re-running stages 1–2 refreshes the data; the crawler resumes
(already-fetched URLs are skipped) and consumes any `unfetched_*.json` queue
files from capped runs.

## What was collected (as of 2026-06-11)

- **2,000+ pages** across 9 ABC domains (raw HTML kept in `data/raw/`)
- **255 clubs** with coordinates, squadron numbers, contacts — from the
  find-a-club search (5 oversized-radius searches; AK/HI/Caribbean/Japan probes
  confirmed completeness because every returned club was already known)
- **148 upcoming classes** at 41 clubs — parsed from all 255 club-details
  pages (7 blank epoch-dated placeholder rows excluded, and said so)
- **10 courses + 30 seminars** from the main-site catalog pages
- **217 Ship's Store products** with item codes and prices (curl-based walk —
  the store's server hangs Python urllib connections), **40 EnrolMart** online
  courses/packages, **13 C.P. Dean** award products
- **1,180 channel videos** (204.5 hours) from per-page schema.org VideoObject
  JSON-LD
- Sitemap `lastmod` freshness data, ABC-to-ABC link graph, 232 local club
  websites, 105 external domains

## Known limitations (deliberate, documented)

- **www.usps.org could not be crawled.** It answered one request at the start
  of the session, then timed out from every network tried (local, browser,
  remote fetcher), and was still down on a 2026-06-13 re-check. The trade-off:
  its pages are absent from the corpus; its inbound/outbound links still appear
  in the link graph. Operator-visible consequence: it shows as **"not crawled"**
  in the Web Properties analytics matrix and platform table (a labeled gap, not a
  blank that reads as "measured, found nothing"). Re-run
  `python3 scrape/crawl.py --domains www.usps.org` when the site is reachable
  again. The same applies to `course.americasboatingcourse.com`, whose root
  returned a server error during the crawl (only `/sign-in` responded).
- **portal.americasboatingclub.org is login-gated** (iMIS). Only its public
  login/news pages (25) were fetched; 20 deeper URLs were left at the page cap
  and are listed in `data/crawl/unfetched_portal.americasboatingclub.org.json`.
- **The two satellite storefronts are snapshots**: the Geiger vessel-examiner
  store reports itself closed as of 05/18/2026 (recorded, with the notice
  parsed from the page); C.P. Dean products come from Shopify's public
  `products.json`, which its robots.txt explicitly welcomes.
- **Schedule placeholders**: a few club pages render blank class rows dated
  "Jan 01 1970" with no title. These are excluded and counted
  (`placeholder_rows_excluded` in `class_schedule.json`).

## Politeness

Per-domain delays (0.8–1.2s; **20s for the Ship's Store** because its
robots.txt asks for it), robots.txt honored everywhere, page caps with
explicit "what was left unfetched" reports, resumable crawls, and a hard
wall-clock limit. The find-a-club searches replay the exact form POST the
page itself makes, a handful of times, seconds apart.

## Deploying (Render)

`render.yaml` defines the dashboard as a Render **static site** publishing
the `dashboard/` directory with no build step (the generated `data.js` is
committed). To set it up: push this repo to GitHub, then in the Render
dashboard choose **New → Blueprint**, select the repo, and approve the
`abc-ecosystem-dashboard` service it finds. Every later `git push` to the
default branch auto-deploys. To publish refreshed data, re-run the pipeline
and commit the regenerated `dashboard/data.js` + `data.json`.

## Layout

```
scrape/    crawlers and interactive capture scripts (stdlib only)
derive/    offline extraction + dashboard-data derivation (stdlib only)
data/      crawl index/log, raw HTML, interactive captures, extracted datasets
dashboard/ index.html (static, no dependencies) + generated data.js/data.json
docs/      screenshots from the verification pass
```
