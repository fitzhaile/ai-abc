#!/usr/bin/env python3
"""
Compute every figure the dashboard displays, from the extracted datasets.

What this script does, in plain English:
  It reads the datasets under data/extracted/ and data/interactive/ and
  computes all the aggregates the dashboard shows — counts, sums, medians,
  per-state tallies, per-month timelines. It writes them to:

      dashboard/data.js    (window.DASHBOARD_DATA = {...}; for the page)
      dashboard/data.json  (the same object, for diffing and inspection)

  THE DASHBOARD NEVER CONTAINS A HAND-TYPED NUMBER. If a figure is on the
  dashboard, it was computed here, from files this script names in its
  provenance block, and re-running this script reproduces it exactly.

Run it directly:  python3 derive/build_dashboard_data.py
Stdlib only — no third-party dependencies.
"""

import html
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACTED = os.path.join(PROJECT_ROOT, "data", "extracted")
INTERACTIVE = os.path.join(PROJECT_ROOT, "data", "interactive")
DASHBOARD_DIR = os.path.join(PROJECT_ROOT, "dashboard")


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def load(*path):
    full = os.path.join(*path)
    with open(full, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Video taxonomy — a CUSTOM, rule-based classification of channel titles.
# This is our own grouping, not the channel's: each category is an ordered
# regex over the (HTML-unescaped) title, the FIRST matching rule wins, and
# anything no rule matches lands in "Unclassified" rather than being forced
# into a bucket. The dashboard shows example titles per category so every
# assignment can be eyeballed against the rules below.
VIDEO_TAXONOMY = [
    ("Classic films & TV shows",
     r"flipper|gilligan|popeye|mchale|sea hunt|mutiny|caine|sand pebbles|"
     r"sea hawk|mister roberts|petticoat|midway|don winslow|dolphin|"
     r"tempest|pirate|buccaneer|\bseason (one|two|three|\d+)\b"),
    ("PSAs & promo spots",
     r"\(:\d+\)|_\d+ ?sec|\bpsa\b|promo|influencer"),
    ("Cruising & partner series",
     r"\bwwtv\b|\bww \d|waves of hope|progressive|\bs\d+ ?e\d+\b|"
     r"\brbff\b|\btcf\b|\bwsf\b|season review"),
    ("Life jackets, ECOS & safety gear",
     r"life jacket|life vest|wear it|wear life|\becos\b|engine cut|"
     r"kill switch|fire extinguisher|carbon monoxide|first aid|flare|"
     r"immersion|dressing"),
    ("Sober boating (BUI)",
     r"\bbui\b|sober|alcohol|impair|drink"),
    ("Paddlesports",
     r"kayak|canoe|paddl|\bsup\b|stand.?up"),
    ("PWC & tow sports",
     r"\bpwc\b|jet ski|water ski|wakeboard|tubing|skier|wake sports|"
     r"wake surf|aquabike"),
    ("Rules, navigation & boat handling",
     r"rules|navigat|buoy|chart|anchor|dock|depart|knot|right of way|"
     r"aids to|steering|pivot|overboard|mooring|\bmob\b|signals|distress|"
     r"mayday|rescue|engine failure|maintenance|arrival"),
    ("Kids & family",
     r"\bkids?\b|family|children|youth|junior"),
    ("Fishing & hunting",
     r"fish|angler|hunt"),
    ("Industry & boat shows",
     r"yacht|simrad|whaler|marine .{0,12}(resort|group)|championship|"
     r"boat show"),
    ("General safety & boating tips",
     r"safe|safety|tips|boat|marina|launch|fuel|weather|storm|hurricane|"
     r"water|river|lake|swim|sail|crew|captain|sea\b|drowning|"
     r"electric shock|mmsi|vhf|\bradio\b|drone|fails"),
]
VIDEO_TAXONOMY = [(name, re.compile(pattern, re.IGNORECASE))
                  for name, pattern in VIDEO_TAXONOMY]
# Spanish-language detection: distinctive Spanish words, or at least two
# Spanish function words (a single "de" can appear in English titles).
SPANISH_STRONG = re.compile(
    r"embarcacion|seguridad|anclaje|amarre|boya|equilibrio|biblioteca|"
    r"inspeccion|recursos|remolque|practicas|navegacion|salvavidas|"
    r"\bcorta\b|asegura|diversion|atun|mejor salir|partida|cinturones",
    re.IGNORECASE)
SPANISH_STOPWORDS = re.compile(
    r"\b(de|la|el|en|del|una|para|con|los|las|tu|y)\b", re.IGNORECASE)
# Many vintage uploads carry only a bare episode title ("THE PINK PEARL"),
# so as a last resort before Unclassified, a classic TV-episode runtime
# (20-35 minutes) assigns them to a bucket whose NAME states the rule.
EPISODE_RUNTIME = (20 * 60, 35 * 60)


def classify_video(title, duration_seconds):
    t = html.unescape(title or "")
    if SPANISH_STRONG.search(t) or len(SPANISH_STOPWORDS.findall(t)) >= 2:
        return "En español"
    for name, pattern in VIDEO_TAXONOMY:
        if pattern.search(t):
            return name
    if EPISODE_RUNTIME[0] <= (duration_seconds or 0) <= EPISODE_RUNTIME[1]:
        return "Vintage TV episodes (by 20–35 min runtime)"
    return "Unclassified"


def money_stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "median": round(statistics.median(values), 2),
        "mean": round(statistics.mean(values), 2),
    }


def main():
    log("Loading extracted + interactive datasets...")
    pages = load(EXTRACTED, "pages.json")
    education = load(EXTRACTED, "education_catalog.json")
    enrolmart = load(EXTRACTED, "enrolmart_products.json")
    videos_d = load(EXTRACTED, "videos.json")
    link_graph = load(EXTRACTED, "link_graph.json")
    content_dates = load(EXTRACTED, "content_dates.json")
    clubs_d = load(INTERACTIVE, "clubs.json")
    schedule_d = load(INTERACTIVE, "class_schedule.json")
    store_d = load(INTERACTIVE, "store_products.json")
    satellite = load(INTERACTIVE, "satellite_stores.json")

    clubs = clubs_d["clubs"]
    classes = schedule_d["classes"]
    store_products = store_d["products"]
    videos = videos_d["videos"]
    catalog = education["catalog"]

    # ------------------------------------------------------------------ clubs
    by_state = Counter(c["state"] for c in clubs if c["state"])
    # Upcoming-class counts per squadron, so the map can show which clubs
    # are actively teaching right now.
    classes_by_sqdno = {e["sqdno"]: e["classes"]
                        for e in schedule_d.get("per_club", [])}
    club_map_points = [
        {"name": c["name"], "state": c["state"], "lat": c["latitude"],
         "lng": c["longitude"],
         "website": bool(c.get("website")),
         "classes": classes_by_sqdno.get(c.get("sqdno"), 0),
         "sqdno": c.get("sqdno")}
        for c in clubs if c.get("latitude") is not None]
    clubs_block = {
        "total": len(clubs),
        "states_with_clubs": len(by_state),
        "by_state": dict(by_state.most_common()),
        "with_website": sum(1 for c in clubs if c.get("website")),
        "with_email": sum(1 for c in clubs if c.get("email")),
        "with_facebook": sum(1 for c in clubs if c.get("facebook")),
        "outside_us_states": [c["name"] for c in clubs if not c["state"]],
        "map_points": club_map_points,
    }
    # Naming eras: the rebrand to "America's Boating Club ..." vs the legacy
    # "... Sail and Power Squadron" names, counted from the directory names.
    def naming_bucket(name):
        n = name.lower().replace("’", "'")
        if "america's boating club" in n:
            return "“America's Boating Club …”"
        if "sail and power squadron" in n:
            return "“… Sail and Power Squadron”"
        if "power squadron" in n:
            return "“… Power Squadron”"
        return "other naming"
    clubs_block["naming"] = dict(Counter(
        naming_bucket(c["name"]) for c in clubs).most_common())

    # Distance from each club to its nearest fellow club (great-circle,
    # 3958.8 = Earth's mean radius in miles for the haversine formula).
    def haversine_miles(a, b):
        lat1, lng1, lat2, lng2 = map(
            math.radians, [a["latitude"], a["longitude"],
                           b["latitude"], b["longitude"]])
        h = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2)
             * math.sin((lng2 - lng1) / 2) ** 2)
        return 3958.8 * 2 * math.asin(math.sqrt(h))
    located = [c for c in clubs if c.get("latitude") is not None
               and c.get("longitude") is not None]
    nn_miles = [min(haversine_miles(a, b) for b in located if b is not a)
                for a in located]
    nn_bands = [("under 10 mi", 0, 10), ("10–25 mi", 10, 25),
                ("25–50 mi", 25, 50), ("50–100 mi", 50, 100),
                ("over 100 mi", 100, float("inf"))]
    clubs_block["nearest_neighbor"] = {
        "clubs_measured": len(nn_miles),
        "median_miles": round(statistics.median(nn_miles), 1),
        "buckets": [[label, sum(1 for d in nn_miles if lo <= d < hi)]
                    for label, lo, hi in nn_bands],
    }
    log(f"Clubs: {clubs_block['total']} total across "
        f"{clubs_block['states_with_clubs']} states; "
        f"{clubs_block['with_website']} have websites; naming split "
        f"{clubs_block['naming']}; median nearest-neighbor distance "
        f"{clubs_block['nearest_neighbor']['median_miles']} mi.")

    # --------------------------------------------------------------- schedule
    classes_by_month = Counter()
    for cl in classes:
        # "Jun 13 2026" -> "2026-06"
        try:
            dt = datetime.strptime(cl["begins"], "%b %d %Y")
            classes_by_month[dt.strftime("%Y-%m")] += 1
        except ValueError:
            classes_by_month["unparsed"] += 1
    title_counts = Counter(cl["title"] for cl in classes)
    state_counts = Counter(cl["club_state"] for cl in classes
                           if cl["club_state"])
    state_by_sqdno = {c.get("sqdno"): c.get("state") for c in clubs}
    most_active = sorted(
        (e for e in schedule_d.get("per_club", []) if e.get("classes")),
        key=lambda e: -e["classes"])[:8]
    schedule_block = {
        "total_upcoming": len(classes),
        "clubs_with_classes": schedule_d["_provenance"]["clubs_with_classes"],
        "clubs_checked": schedule_d["_provenance"]["clubs_checked"],
        "kinds": dict(Counter(cl["kind"] or "unknown" for cl in classes)),
        "by_month": dict(sorted(classes_by_month.items())),
        "top_titles": title_counts.most_common(12),
        "by_state": dict(state_counts.most_common()),
        "most_active_clubs": [
            {"club": e["club"], "state": state_by_sqdno.get(e["sqdno"]),
             "classes": e["classes"]} for e in most_active],
    }
    log(f"Schedule: {schedule_block['total_upcoming']} upcoming classes at "
        f"{schedule_block['clubs_with_classes']} of "
        f"{schedule_block['clubs_checked']} clubs.")

    # -------------------------------------------------------------- education
    # Join the catalog against the live schedule: which of the 40 catalog
    # items actually have a class on the calendar somewhere in the country?
    def edu_path(url):
        path = (url or "").split("americasboatingclub.org", 1)[-1]
        return path.replace("/index.php", "", 1).rstrip("/")
    catalog_by_path = {edu_path(item["url"]): item for item in catalog}
    catalog_by_title = {item["title"].lower(): item for item in catalog}
    for item in catalog:
        item["upcoming_classes"] = 0
    outside_catalog = Counter()
    for cl in classes:
        item = (catalog_by_path.get(edu_path(cl.get("course_url")))
                or catalog_by_title.get((cl.get("title") or "").lower()))
        if item:
            item["upcoming_classes"] += 1
        else:
            outside_catalog[cl.get("title") or "untitled"] += 1
    items_with_upcoming = sum(1 for i in catalog if i["upcoming_classes"])
    coverage = {
        "items_with_upcoming": items_with_upcoming,
        "items_dormant": len(catalog) - items_with_upcoming,
        "classes_matching_catalog": sum(i["upcoming_classes"]
                                        for i in catalog),
        "classes_outside_catalog": sum(outside_catalog.values()),
        "outside_catalog_titles": outside_catalog.most_common(5),
    }
    log(f"Education coverage: {items_with_upcoming} of {len(catalog)} "
        f"catalog items have at least one upcoming class; "
        f"{coverage['classes_outside_catalog']} scheduled classes teach "
        f"offerings outside the catalog "
        f"(top: {coverage['outside_catalog_titles'][:2]}).")

    edu_by_cat = defaultdict(lambda: {"courses": [], "seminars": []})
    for item in catalog:
        edu_by_cat[item["category"]][item["kind"] + "s"].append(item["title"])
    education_block = {
        "courses": education["counts"]["courses"],
        "seminars": education["counts"]["seminars"],
        "by_category": {cat: v for cat, v in sorted(edu_by_cat.items())},
        "catalog_coverage": coverage,
        "catalog": catalog,
    }

    # --------------------------------------------------------------- commerce
    store_prices = [p["price_min"] for p in store_products]
    # Top-level category = first segment under /Catalog/ that the product
    # was listed under (a product can appear in several).
    store_by_category = Counter()
    for p in store_products:
        cats = {u.strip("/").split("/")[-1].replace("-", " ")
                for u in p["listed_under"]}
        for cat in cats:
            store_by_category[cat or "Catalog root"] += 1
    # Price bands for the Ship's Store (by each product's lowest price).
    price_bands = [("under $10", 0, 10), ("$10–25", 10, 25),
                   ("$25–50", 25, 50), ("$50–100", 50, 100),
                   ("$100 and up", 100, float("inf"))]
    priced_values = [p["price_min"] for p in store_products
                     if p["price_min"] is not None]
    price_histogram = [[label, sum(1 for v in priced_values if lo <= v < hi)]
                       for label, lo, hi in price_bands]
    enrol_products = enrolmart["products"]
    cpdean = satellite["cpdean_awards_collection"]["products"]
    commerce_block = {
        "ships_store": {
            "products": len(store_products),
            "price_stats": money_stats(store_prices),
            "price_histogram": price_histogram,
            "unpriced_items": len(store_products) - len(priced_values),
            "by_category": dict(store_by_category.most_common()),
            "items": [{"name": p["name"], "price_min": p["price_min"],
                       "price_max": p["price_max"],
                       "item_code": p["item_code"], "url": p["url"]}
                      for p in store_products],
        },
        "enrolmart": {
            "products": len(enrol_products),
            "online_courses": sum(1 for p in enrol_products
                                  if p["type"] == "online course"),
            "packages": sum(1 for p in enrol_products
                            if p["type"] == "package"),
            "price_stats": money_stats([p["price_usd"]
                                        for p in enrol_products]),
            "items": enrol_products,
        },
        "cpdean_awards": {
            "products": len(cpdean),
            "price_stats": money_stats([p["price_min"] for p in cpdean]),
            "items": cpdean,
        },
        "geiger_vessel_examiner": satellite["geiger_vessel_examiner_store"],
    }
    log(f"Commerce: {commerce_block['ships_store']['products']} Ship's Store "
        f"products, {commerce_block['enrolmart']['products']} EnrolMart "
        f"products, {commerce_block['cpdean_awards']['products']} C.P. Dean "
        f"award items; Geiger store status: "
        f"{satellite['geiger_vessel_examiner_store']['status']}.")

    # ----------------------------------------------------------------- videos
    uploads_by_year = Counter((v["upload_date"] or "")[:4] or "unknown"
                              for v in videos)
    durations = [v["duration_seconds"] for v in videos
                 if v["duration_seconds"]]
    buckets = Counter()
    for s in durations:
        if s < 120: buckets["under 2 min"] += 1
        elif s < 300: buckets["2-5 min"] += 1
        elif s < 600: buckets["5-10 min"] += 1
        elif s < 1200: buckets["10-20 min"] += 1
        else: buckets["over 20 min"] += 1
    # Per-year picture: hours added, running total, and the median runtime
    # of that year's uploads (medians resist the long classic films).
    per_year_secs = defaultdict(list)
    for v in videos:
        year = (v["upload_date"] or "")[:4]
        if year:
            per_year_secs[year].append(v["duration_seconds"] or 0)
    by_year = {}
    running_hours = 0.0
    for year in sorted(per_year_secs):
        secs = per_year_secs[year]
        hours = sum(secs) / 3600
        running_hours += hours
        with_duration = [s for s in secs if s]
        by_year[year] = {
            "videos": len(secs),
            "hours": round(hours, 1),
            "cumulative_hours": round(running_hours, 1),
            "median_minutes": (round(statistics.median(with_duration) / 60, 1)
                               if with_duration else None),
        }
    # Feature-length vs short-form: a transparent rule (60 minutes or more
    # counts as feature-length) splits the classic films from the original
    # safety shorts.
    total_hours = sum(durations) / 3600
    feature_secs = [s for s in durations if s >= 3600]
    length_mix = {
        "rule": "runtime of 60 minutes or more counts as feature-length",
        "feature_videos": len(feature_secs),
        "feature_hours": round(sum(feature_secs) / 3600, 1),
        "short_videos": len(videos) - len(feature_secs),
        "short_hours": round(total_hours - sum(feature_secs) / 3600, 1),
        "feature_share_of_runtime_pct": (
            round(sum(feature_secs) / 3600 / total_hours * 100, 1)
            if total_hours else None),
    }

    # Custom taxonomy over titles (rules at the top of this file).
    tax_counts = Counter()
    tax_hours = defaultdict(float)
    tax_examples = defaultdict(list)
    for v in videos:
        cat = classify_video(v["name"], v["duration_seconds"])
        tax_counts[cat] += 1
        tax_hours[cat] += (v["duration_seconds"] or 0) / 3600
        if len(tax_examples[cat]) < 3:
            tax_examples[cat].append(html.unescape(v["name"] or ""))
    taxonomy = {
        "method": ("Our own grouping, not the channel's: ordered keyword "
                   "rules over titles, first match wins; Spanish detected "
                   "by Spanish words; bare titles with a classic 20-35 min "
                   "episode runtime are bucketed as vintage TV. Whatever "
                   "no rule matches stays Unclassified — mostly one-off "
                   "story features and bare cartoon/serial titles with no "
                   "keyword signal. Rules live in "
                   "derive/build_dashboard_data.py."),
        "categories": [
            {"name": name, "videos": n,
             "hours": round(tax_hours[name], 1),
             "examples": tax_examples[name]}
            for name, n in tax_counts.most_common()],
    }
    log(f"Video taxonomy: " + ", ".join(
        f"{c['name']} {c['videos']}" for c in taxonomy["categories"]) + ".")
    log(f"Length mix: {length_mix['feature_videos']} feature-length videos "
        f"hold {length_mix['feature_hours']}h "
        f"({length_mix['feature_share_of_runtime_pct']}% of all runtime).")

    videos_block = {
        "total": len(videos),
        "by_year": by_year,
        "length_mix": length_mix,
        "taxonomy": taxonomy,
        "total_runtime_hours": round(sum(durations) / 3600, 1),
        "median_duration_minutes": (round(statistics.median(durations) / 60, 1)
                                    if durations else None),
        "uploads_by_year": dict(sorted(uploads_by_year.items())),
        "duration_buckets": [
            [k, buckets[k]] for k in
            ["under 2 min", "2-5 min", "5-10 min", "10-20 min", "over 20 min"]
            if k in buckets],
        "longest": sorted(
            [{"name": v["name"], "minutes": round(v["duration_seconds"] / 60, 1)}
             for v in videos if v["duration_seconds"]],
            key=lambda v: -v["minutes"])[:8],
    }
    log(f"Videos: {videos_block['total']} videos, "
        f"{videos_block['total_runtime_hours']} hours of content.")

    # ------------------------------------------------------------- crawl/site
    per_domain = pages["per_domain"]
    pages_total = sum(d.get("fetched", 0) for d in per_domain.values())
    failures_total = sum(d.get("failed", 0) for d in per_domain.values())
    lastmod_by_year = Counter((e["lastmod"] or "")[:4] or "unknown"
                              for e in content_dates["entries"])
    broken = pages.get("broken_links", [])
    def pretty_link_source(src):
        if src.startswith("sitemap:"):
            return "the site's own sitemap"
        if src == "unfetched queue from a previous run":
            return "an earlier crawl pass"
        return src
    from_sitemap = sum(1 for b in broken
                       if b["linked_from"].startswith("sitemap:"))
    log(f"Broken links: {len(broken)} URLs 404ed when followed; "
        f"{from_sitemap} of them are advertised by a site's own sitemap.")
    site_block = {
        "domains_crawled": len(per_domain),
        "pages_fetched": pages_total,
        "fetch_failures": failures_total,
        "per_domain": per_domain,
        "main_site_lastmod_by_year": dict(sorted(lastmod_by_year.items())),
        "broken_links": {
            "total": len(broken),
            "advertised_by_sitemap": from_sitemap,
            "by_domain": dict(Counter(b["domain"] for b in broken)),
            "top": [{"url": b["url"],
                     "linked_from": pretty_link_source(b["linked_from"])}
                    for b in broken[:12]],
        },
        "notes": [
            "www.usps.org (the parent organization's legacy site) answered "
            "one request at the start of the session, then became "
            "unreachable from every network tried (local client, browser, "
            "and a remote fetcher all timed out), so its pages could not "
            "be collected — the site appears to be down or heavily "
            "throttled. Its links are still counted in the link graph.",
            "portal.americasboatingclub.org is a members-only iMIS portal; "
            "only its public login/news pages were collected.",
            "store.shopusps.org's robots.txt asks crawlers to wait 20 "
            "seconds between requests, which was honored.",
        ],
    }

    # -------------------------------------------------------------- ecosystem
    ecosystem_block = {
        "abc_cross_links": link_graph["abc_cross_links"],
        "club_websites_total": len(link_graph["club_websites"]),
        "external_domains_total": link_graph["external_domains_total"],
        "top_external_domains": dict(list(
            link_graph["external_domains_top100"].items())[:25]),
    }

    # ------------------------------------------------------------- provenance
    provenance_block = {
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "generator": "derive/build_dashboard_data.py",
        "pipeline": [
            {"stage": "crawl", "script": "scrape/crawl.py",
             "evidence": "data/crawl/index.jsonl + data/raw/<domain>/*.html"},
            {"stage": "interactive captures",
             "script": ("scrape/fetch_club_locations.py, "
                        "scrape/fetch_club_schedules.py, "
                        "scrape/fetch_store_catalog.py, "
                        "scrape/fetch_satellite_stores.py"),
             "evidence": "data/interactive/*.json (+ raw responses/HTML)"},
            {"stage": "extract", "script": "derive/extract.py",
             "evidence": "data/extracted/*.json"},
            {"stage": "derive", "script": "derive/build_dashboard_data.py",
             "evidence": "dashboard/data.json (this file's source)"},
        ],
        "dataset_sources": {
            "clubs": clubs_d["_provenance"],
            "class_schedule": schedule_d["_provenance"],
            "store_products": store_d["_provenance"],
            "satellite_stores": satellite["_provenance"],
            "education_catalog": education["_provenance"],
            "enrolmart_products": enrolmart["_provenance"],
            "videos": videos_d["_provenance"],
            "pages": pages["_provenance"],
            "link_graph": link_graph["_provenance"],
            "content_dates": content_dates["_provenance"],
        },
    }
    # Strip the bulky per-search logs out of the dashboard copy (they stay in
    # the source files); keep the descriptive fields.
    clubs_prov = dict(provenance_block["dataset_sources"]["clubs"])
    clubs_prov.pop("searches", None)
    provenance_block["dataset_sources"]["clubs"] = clubs_prov
    store_prov = dict(provenance_block["dataset_sources"]["store_products"])
    store_prov.pop("pages_visited", None)
    provenance_block["dataset_sources"]["store_products"] = store_prov

    # The per-club schedule source is a URL *pattern* (?sqdno=<squadron
    # number>); fill the placeholder with a real squadron number from the
    # data so the Sources panel can show a working, clickable example.
    sched_prov = dict(provenance_block["dataset_sources"]["class_schedule"])
    example_sqdno = next(
        (e["sqdno"] for e in schedule_d.get("per_club", []) if e.get("classes")),
        clubs[0].get("sqdno") if clubs else None)
    pattern = sched_prov.get("source_url_pattern", "")
    if example_sqdno and "<sqdno>" in pattern:
        sched_prov["example_url"] = pattern.replace("<sqdno>",
                                                    str(example_sqdno))
        sched_prov["description"] += (
            " The link below opens one real club's page; the sqdno query "
            "parameter is that club's squadron number — change it to view "
            "any other club's schedule.")
    provenance_block["dataset_sources"]["class_schedule"] = sched_prov

    # Give every dataset a uniform "links" list: the live URLs its evidence
    # was fetched from. Each provenance flavor records them under a
    # different key (source_url, example_url for filled-in patterns,
    # web_sources, or a sources list that mixes URLs with local file
    # paths) — collect only actual URLs, never invent one.
    for name, prov in provenance_block["dataset_sources"].items():
        prov = dict(prov)
        links = []
        if prov.get("source_url"):
            links.append(prov["source_url"])
        if prov.get("example_url"):
            links.append(prov["example_url"])
        elif prov.get("source_url_pattern"):
            links.append(prov["source_url_pattern"])
        links.extend(prov.get("web_sources", []))
        links.extend(s for s in prov.get("sources", [])
                     if isinstance(s, str) and s.startswith("http"))
        seen = set()
        prov["links"] = [u for u in links
                         if not (u in seen or seen.add(u))]
        if not prov["links"]:
            log(f"  note: dataset '{name}' has no web-source links in its "
                f"provenance — the Sources panel will say so instead of "
                f"showing a link.")
        provenance_block["dataset_sources"][name] = prov

    data = {
        "clubs": clubs_block,
        "schedule": schedule_block,
        "education": education_block,
        "commerce": commerce_block,
        "videos": videos_block,
        "site": site_block,
        "ecosystem": ecosystem_block,
        "provenance": provenance_block,
    }

    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    json_path = os.path.join(DASHBOARD_DIR, "data.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    js_path = os.path.join(DASHBOARD_DIR, "data.js")
    with open(js_path, "w", encoding="utf-8") as fh:
        fh.write("// GENERATED by derive/build_dashboard_data.py — do not "
                 "edit by hand.\n// Regenerate with: python3 "
                 "derive/build_dashboard_data.py\n")
        fh.write("window.DASHBOARD_DATA = ")
        json.dump(data, fh, ensure_ascii=False)
        fh.write(";\n")
    log(f"Wrote {json_path} and {js_path}. Every number the dashboard shows "
        f"comes from these files.")


if __name__ == "__main__":
    main()
