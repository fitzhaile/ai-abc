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

import json
import os
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
    club_map_points = [
        {"name": c["name"], "state": c["state"], "lat": c["latitude"],
         "lng": c["longitude"],
         "website": bool(c.get("website")),
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
    log(f"Clubs: {clubs_block['total']} total across "
        f"{clubs_block['states_with_clubs']} states; "
        f"{clubs_block['with_website']} have websites.")

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
    schedule_block = {
        "total_upcoming": len(classes),
        "clubs_with_classes": schedule_d["_provenance"]["clubs_with_classes"],
        "clubs_checked": schedule_d["_provenance"]["clubs_checked"],
        "kinds": dict(Counter(cl["kind"] or "unknown" for cl in classes)),
        "by_month": dict(sorted(classes_by_month.items())),
        "top_titles": title_counts.most_common(12),
        "by_state": dict(state_counts.most_common()),
    }
    log(f"Schedule: {schedule_block['total_upcoming']} upcoming classes at "
        f"{schedule_block['clubs_with_classes']} of "
        f"{schedule_block['clubs_checked']} clubs.")

    # -------------------------------------------------------------- education
    edu_by_cat = defaultdict(lambda: {"courses": [], "seminars": []})
    for item in catalog:
        edu_by_cat[item["category"]][item["kind"] + "s"].append(item["title"])
    education_block = {
        "courses": education["counts"]["courses"],
        "seminars": education["counts"]["seminars"],
        "by_category": {cat: v for cat, v in sorted(edu_by_cat.items())},
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
    enrol_products = enrolmart["products"]
    cpdean = satellite["cpdean_awards_collection"]["products"]
    commerce_block = {
        "ships_store": {
            "products": len(store_products),
            "price_stats": money_stats(store_prices),
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
    videos_block = {
        "total": len(videos),
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
    site_block = {
        "domains_crawled": len(per_domain),
        "pages_fetched": pages_total,
        "fetch_failures": failures_total,
        "per_domain": per_domain,
        "main_site_lastmod_by_year": dict(sorted(lastmod_by_year.items())),
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

    # Give every dataset a uniform "links" list: the live URLs its evidence
    # was fetched from. Each provenance flavor records them under a
    # different key (source_url, source_url_pattern, web_sources, or a
    # sources list that mixes URLs with local file paths) — collect only
    # actual URLs, never invent one.
    for name, prov in provenance_block["dataset_sources"].items():
        prov = dict(prov)
        links = []
        for key in ("source_url", "source_url_pattern"):
            if prov.get(key):
                links.append(prov[key])
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
