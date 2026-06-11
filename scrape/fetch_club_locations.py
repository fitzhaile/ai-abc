#!/usr/bin/env python3
"""
Fetch the complete America's Boating Club club/squadron directory from the
"Find a Club Near You" search on americasboatingclub.org.

How this works, in plain English:
  The find-a-club page is a Joomla com_mymaplocations search. The browser
  POSTs a latitude/longitude/radius form and gets back GeoJSON of nearby
  clubs (this was observed by watching the page's own network traffic).
  The search UI caps the radius at 500 miles, so one search cannot see the
  whole country.

  This script first PROBES whether the server honors a radius larger than
  the UI offers (many installs do). If yes, it uses a handful of giant
  circles; if no, it falls back to a 24-circle grid that blankets the
  continental US, Alaska, Hawaii, Puerto Rico, Guam and Japan (overlapping
  500-mile circles, so no gaps).

  Every raw response is saved for provenance, then features are merged by
  their server-side id, the address HTML is parsed into structured fields,
  and the result is written to data/interactive/clubs.json.

Run it directly:  python3 scrape/fetch_club_locations.py
Stdlib only — no third-party dependencies.
"""

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(PROJECT_ROOT, "data", "interactive")
RAW_DIR = os.path.join(OUT_DIR, "club_search_responses")
CLUBS_PATH = os.path.join(OUT_DIR, "clubs.json")

SEARCH_URL = "https://americasboatingclub.org/index.php/find-a-club-near-you"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DELAY_SECONDS = 2.5  # pause between searches; this is a handful of requests

# Fallback grid: overlapping 500-mile circles covering every US state,
# territory, and the overseas locations where USPS units have existed.
GRID_CENTERS = [
    # (label, lat, lng)
    ("US South row", 30, -120), ("US South row", 30, -110),
    ("US South row", 30, -100), ("US South row", 30, -90),
    ("US South row", 30, -81),
    ("US Middle row", 38, -122), ("US Middle row", 38, -112),
    ("US Middle row", 38, -102), ("US Middle row", 38, -92),
    ("US Middle row", 38, -82), ("US Middle row", 38, -74),
    ("US North row", 46, -122), ("US North row", 46, -112),
    ("US North row", 46, -102), ("US North row", 46, -92),
    ("US North row", 46, -82), ("US North row", 46, -72),
    ("South Florida", 26, -81),
    ("Puerto Rico / USVI", 18.3, -66.5),
    ("Alaska (Anchorage)", 61.2, -149.9),
    ("Alaska (Southeast)", 58.0, -135.0),
    ("Hawaii", 21.3, -157.9),
    ("Guam", 13.5, 144.8),
    ("Japan (Tokyo Bay)", 35.5, 139.8),
]

# Big-circle strategy used when the server accepts an oversized radius.
BIG_CENTERS = [
    ("CONUS center, huge radius", 39.0, -98.0),
    ("Pacific (HI/Guam), huge radius", 21.3, -157.9),
    ("Alaska, huge radius", 61.2, -149.9),
    ("Caribbean, huge radius", 18.3, -66.5),
    ("Japan, huge radius", 35.5, 139.8),
]
BIG_RADIUS = 3000


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def search(lat, lng, radius):
    """One club search POST, exactly as the page itself does it."""
    form = {
        "searchzip": "scripted-grid-search",
        "task": "search",
        "radius": str(radius),
        "option": "com_mymaplocations",
        "limit": "0",
        "component": "com_mymaplocations",
        "Itemid": "135",
        "zoom": "5",
        "format": "json",
        "geo": "1",
        "limitstart": "0",
        "latitude": str(lat),
        "longitude": str(lng),
    }
    req = urllib.request.Request(
        SEARCH_URL,
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def real_features(geojson):
    """Drop the synthetic 'You' marker (id 0) the server adds for the map."""
    return [f for f in geojson.get("features", [])
            if f.get("id") and f.get("properties", {}).get("name") != "You"]


FIELD_PATTERNS = {
    "phone": re.compile(r'href="tel:([^"]+)"'),
    "email": re.compile(r"href='mailto:([^']+)'"),
    "facebook": re.compile(r"href='(https?://(?:www\.)?facebook\.com[^']*)'",
                           re.IGNORECASE),
    "website": re.compile(r"href='([^']+)'[^>]*>\s*Visit Club Web Site"),
    "sqdno": re.compile(r"sqdno=(\d+)"),
    "burgee": re.compile(r"src=(https?://\S+?\.(?:png|gif|jpe?g))",
                         re.IGNORECASE),
}
STATE_ZIP = re.compile(r"&nbsp;([A-Z]{2})&nbsp;(\d{5})")
COUNTRY = re.compile(r"<br/>([A-Za-z .]{3,30})<br/>")


def parse_club(feature):
    props = feature.get("properties", {})
    raw = props.get("fulladdress", "") or props.get("description", "")
    club = {
        "location_id": feature.get("id"),
        "name": html.unescape(props.get("name", "")).strip(),
        "longitude": feature.get("geometry", {}).get("coordinates", [None, None])[0],
        "latitude": feature.get("geometry", {}).get("coordinates", [None, None])[1],
        "detail_url": ("https://americasboatingclub.org" + props["url"])
        if props.get("url") else None,
    }
    for key, pattern in FIELD_PATTERNS.items():
        match = pattern.search(raw)
        club[key] = html.unescape(match.group(1)).strip() if match else None
    state_zip = STATE_ZIP.search(raw)
    club["state"] = state_zip.group(1) if state_zip else None
    club["zip"] = state_zip.group(2) if state_zip else None
    country = COUNTRY.search(raw)
    club["country"] = country.group(1).strip() if country else None
    club["raw_address_html"] = raw
    return club


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ---- Probe: does the server honor a radius bigger than the UI's 500? ----
    log("Probe 1/2: searching from the geographic center of the US with the "
        "UI's maximum radius (500 mi)...")
    baseline = real_features(search(39.0, -98.0, 500))
    log(f"  -> {len(baseline)} clubs within 500 mi of the US center.")
    time.sleep(DELAY_SECONDS)
    log(f"Probe 2/2: same center with an oversized radius ({BIG_RADIUS} mi) "
        f"to see if the server honors it...")
    big = real_features(search(39.0, -98.0, BIG_RADIUS))
    log(f"  -> {len(big)} clubs with radius {BIG_RADIUS}.")
    time.sleep(DELAY_SECONDS)

    if len(big) > len(baseline):
        strategy = (f"oversized-radius circles (server honors radius "
                    f"{BIG_RADIUS} mi beyond the UI's 500 mi cap)")
        searches = [(label, lat, lng, BIG_RADIUS)
                    for label, lat, lng in BIG_CENTERS]
        log(f"The server honors the oversized radius ({len(big)} > "
            f"{len(baseline)} clubs), so {len(searches)} giant circles "
            f"will cover everything.")
    else:
        strategy = ("24-circle grid of 500 mi searches (server caps the "
                    "radius at the UI maximum)")
        searches = [(label, lat, lng, 500)
                    for label, lat, lng in GRID_CENTERS]
        log(f"The oversized radius returned no extra clubs ({len(big)} vs "
            f"{len(baseline)}), so the full {len(searches)}-circle grid "
            f"will be used instead.")

    merged = {}
    search_log = []
    for i, (label, lat, lng, radius) in enumerate(searches, 1):
        log(f"Search {i}/{len(searches)}: {label} "
            f"(lat {lat}, lng {lng}, radius {radius} mi)...")
        try:
            geojson = search(lat, lng, radius)
        except Exception as exc:
            log(f"  -> FAILED: {exc.__class__.__name__}: {exc}. Continuing "
                f"with the other circles; coverage for this area may be "
                f"incomplete — re-run the script to retry.")
            search_log.append({"label": label, "lat": lat, "lng": lng,
                               "radius": radius,
                               "outcome": f"failed: {exc}"})
            time.sleep(DELAY_SECONDS)
            continue
        feats = real_features(geojson)
        raw_name = f"{str(lat).replace('.', 'p')}_{str(lng).replace('.', 'p')}_r{radius}.json"
        with open(os.path.join(RAW_DIR, raw_name), "w", encoding="utf-8") as fh:
            json.dump(geojson, fh)
        new = 0
        for feat in feats:
            if feat["id"] not in merged:
                merged[feat["id"]] = feat
                new += 1
        log(f"  -> {len(feats)} clubs returned, {new} not seen before "
            f"(running total: {len(merged)}). Raw response saved to "
            f"club_search_responses/{raw_name}")
        search_log.append({"label": label, "lat": lat, "lng": lng,
                           "radius": radius, "returned": len(feats),
                           "new": new, "raw_file": raw_name,
                           "outcome": "ok"})
        time.sleep(DELAY_SECONDS)

    clubs = [parse_club(f) for f in merged.values()]
    clubs.sort(key=lambda c: (c.get("state") or "ZZ", c["name"]))

    parsed_ok = sum(1 for c in clubs if c["state"])
    log(f"Parsed {len(clubs)} unique clubs; {parsed_ok} have a recognizable "
        f"US state+ZIP in their address (the rest are likely overseas units "
        f"or have incomplete addresses — inspect raw_address_html for those).")

    output = {
        "_provenance": {
            "description": (
                "Complete club/squadron directory obtained by replaying the "
                "find-a-club search (Joomla com_mymaplocations) that the "
                "americasboatingclub.org page itself performs, across "
                "overlapping circles that blanket all US states, territories "
                "and overseas locations."),
            "source_url": SEARCH_URL,
            "script": "scrape/fetch_club_locations.py",
            "strategy": strategy,
            "fetched_at": started,
            "searches": search_log,
            "unique_clubs": len(clubs),
        },
        "clubs": clubs,
    }
    with open(CLUBS_PATH, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=1, ensure_ascii=False)
    log(f"Done. {len(clubs)} unique clubs written to {CLUBS_PATH}")


if __name__ == "__main__":
    main()
