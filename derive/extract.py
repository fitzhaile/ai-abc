#!/usr/bin/env python3
"""
Extract structured datasets from the raw crawl + interactive captures.

What this script does, in plain English:
  It reads ONLY what is already on disk (data/crawl/index.jsonl, the raw
  HTML in data/raw/, the sitemap XML copies in data/crawl/, and the
  interactive captures in data/interactive/) and turns them into clean
  datasets under data/extracted/. It performs no network requests, so it
  can be re-run any time to rebuild every dataset from the same evidence.

Datasets written to data/extracted/:
  pages.json              one slim record per crawled URL + per-domain stats
  education_catalog.json  every course and seminar page on the main site
  enrolmart_products.json online courses/packages with prices from EnrolMart
  videos.json             every America's Boating Channel video (from each
                          program page's schema.org VideoObject JSON-LD)
  link_graph.json         ABC-property-to-ABC-property links, external
                          domains, and the local club website list
  content_dates.json      per-URL lastmod dates from the saved sitemap XML

Every dataset includes a _provenance block saying where its facts came from.
Run it directly:  python3 derive/extract.py
Stdlib only — no third-party dependencies.
"""

import html as html_mod
import importlib.util
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(PROJECT_ROOT, "data", "crawl", "index.jsonl")
CRAWL_DIR = os.path.join(PROJECT_ROOT, "data", "crawl")
OUT_DIR = os.path.join(PROJECT_ROOT, "data", "extracted")

# Reuse the crawler's host->domain map so "what counts as an ABC property"
# has exactly one definition in the whole project.
_spec = importlib.util.spec_from_file_location(
    "crawl", os.path.join(PROJECT_ROOT, "scrape", "crawl.py"))
_crawl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_crawl)
HOST_TO_DOMAIN = _crawl.HOST_TO_DOMAIN


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def provenance(description, sources, web_sources=None):
    """web_sources: the live URLs the underlying evidence was fetched from
    (computed from the data itself, never typed from memory)."""
    block = {"description": description, "script": "derive/extract.py",
             "extracted_at": now_iso(), "sources": sources}
    if web_sources:
        block["web_sources"] = sorted(set(web_sources))
    return block


def read_html(record):
    path = os.path.join(PROJECT_ROOT, record["html_path"])
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def load_index():
    """Read index.jsonl keeping the LAST record per normalized URL (re-runs
    may have appended fresher records for the same page)."""
    records = {}
    bad_lines = 0
    with open(INDEX_PATH, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            records[r.get("normalized_url") or r.get("url")] = r
    if bad_lines:
        log(f"index.jsonl: {bad_lines} unparseable lines ignored (probably "
            f"a record cut short when a crawl was force-killed).")
    return list(records.values())


META_DESC = re.compile(
    r'<meta (?:name="description"|property="og:description") '
    r'content="([^"]*)"', re.IGNORECASE)
H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
TITLE = re.compile(r"<title>([^<]*)</title>")


def text_of(markup):
    return html_mod.unescape(re.sub(r"<[^>]+>", "", markup)).strip()


# ---------------------------------------------------------------------------
# pages.json
# ---------------------------------------------------------------------------
def extract_pages(records):
    pages = []
    domain_stats = defaultdict(lambda: Counter())
    for r in records:
        domain_stats[r["domain"]][
            "fetched" if r.get("html_path") else "other"] += 1
        outcome = r.get("outcome", "")
        if outcome.startswith("failed"):
            domain_stats[r["domain"]]["failed"] += 1
        elif outcome.startswith("skipped"):
            domain_stats[r["domain"]]["skipped"] += 1
        elif r.get("is_pdf"):
            domain_stats[r["domain"]]["pdf_recorded"] += 1
        pages.append({
            "url": r.get("final_url") or r["url"],
            "domain": r["domain"],
            "title": r.get("title"),
            "http_status": r.get("http_status"),
            "outcome": outcome,
            "depth": r.get("depth"),
            "bytes": r.get("bytes"),
            "fetched_at": r.get("fetched_at"),
        })
    stats = {d: dict(c) for d, c in sorted(domain_stats.items())}
    for d, c in stats.items():
        log(f"  {d}: {c.get('fetched', 0)} pages fetched, "
            f"{c.get('failed', 0)} failed, {c.get('skipped', 0)} skipped, "
            f"{c.get('pdf_recorded', 0)} PDFs recorded as links.")
    return {
        "_provenance": provenance(
            "One record per URL the crawler handled, with per-domain "
            "totals. Source of truth is data/crawl/index.jsonl.",
            ["data/crawl/index.jsonl"],
            web_sources=[f"https://{d}/" for d in stats]),
        "per_domain": stats,
        "pages": pages,
    }


# ---------------------------------------------------------------------------
# education_catalog.json — courses & seminars on americasboatingclub.org
# ---------------------------------------------------------------------------
EDU_PATH = re.compile(
    r"https://americasboatingclub\.org/index\.php/"
    r"(courses|seminars)/([a-z0-9-]+)/([a-z0-9-]+)$")


def extract_education(records):
    items = {}
    for r in records:
        if r["domain"] != "americasboatingclub.org" or not r.get("html_path"):
            continue
        m = EDU_PATH.match(r.get("normalized_url") or "")
        if not m:
            continue
        kind, category_slug, slug = m.groups()
        page = read_html(r)
        if page is None:
            log(f"  WARNING: raw HTML missing for {r['url']} — page counted "
                f"but title/description unavailable.")
        h1 = None
        desc = None
        if page:
            h1m = re.search(
                r'<h1 class="header-(?:courses|seminars)">(.*?)</h1>',
                page, re.DOTALL)
            h1 = text_of(h1m.group(1)) if h1m else None
            if not h1:
                h1m = H1.search(page)
                h1 = text_of(h1m.group(1)) if h1m else None
            dm = META_DESC.search(page)
            desc = html_mod.unescape(dm.group(1)).strip() if dm else None
        key = (kind, category_slug, slug)
        items[key] = {
            "kind": kind.rstrip("s"),  # course / seminar
            "title": h1 or r.get("title") or slug.replace("-", " ").title(),
            "category": category_slug.replace("-", " ").title(),
            "category_slug": category_slug,
            "slug": slug,
            "url": r.get("final_url") or r["url"],
            "description": desc,
        }
    catalog = sorted(items.values(),
                     key=lambda x: (x["kind"], x["category"], x["title"]))
    n_courses = sum(1 for c in catalog if c["kind"] == "course")
    n_seminars = sum(1 for c in catalog if c["kind"] == "seminar")
    no_desc = sum(1 for c in catalog if not c["description"])
    log(f"  education catalog: {n_courses} courses and {n_seminars} seminars "
        f"found ({no_desc} pages had no meta description — left null, not "
        f"invented).")
    edu_prefixes = []
    for item in catalog:
        m = re.match(r"(https://[^/]+/index\.php/(?:courses|seminars)/)",
                     item["url"])
        if m:
            edu_prefixes.append(m.group(1))
    return {
        "_provenance": provenance(
            "Every page under /index.php/courses/<category>/<name> and "
            "/index.php/seminars/<category>/<name> on the main site. "
            "Titles come from each page's H1, descriptions from its meta "
            "description tag.",
            ["data/crawl/index.jsonl", "data/raw/americasboatingclub.org/"],
            web_sources=edu_prefixes),
        "counts": {"courses": n_courses, "seminars": n_seminars},
        "catalog": catalog,
    }


# ---------------------------------------------------------------------------
# enrolmart_products.json
# ---------------------------------------------------------------------------
PRICE_H2 = re.compile(r'<h2 class="product-price">\s*\$([\d,.]+)\s*</h2>')


def extract_enrolmart(records):
    products = []
    seen = set()
    for r in records:
        if r["domain"] != "uspsonline.enrolmart.com" or not r.get("html_path"):
            continue
        url = r.get("normalized_url") or r["url"]
        pm = re.match(
            r"https://uspsonline\.enrolmart\.com/"
            r"(online-courses|course-packages|seminars[a-z-]*|[a-z-]+)/"
            r"([a-z0-9-]+)$", url)
        if not pm:
            continue
        page = read_html(r)
        if not page:
            continue
        price_m = PRICE_H2.search(page)
        if not price_m:
            continue  # listing/info page, not a product page
        h1m = H1.search(page)
        title = text_of(h1m.group(1)) if h1m else r.get("title")
        if url in seen:
            continue
        seen.add(url)
        section = pm.group(1)
        products.append({
            "title": title,
            "price_usd": float(price_m.group(1).replace(",", "")),
            "section": section,
            "type": ("package" if section == "course-packages"
                     else "online course"),
            "url": url,
        })
    products.sort(key=lambda p: (p["type"], p["title"]))
    log(f"  enrolmart: {len(products)} priced products "
        f"({sum(1 for p in products if p['type'] == 'package')} packages, "
        f"{sum(1 for p in products if p['type'] == 'online course')} online "
        f"courses).")
    enrol_roots = []
    for p in products:
        m = re.match(r"(https://[^/]+/[a-z-]+/)", p["url"])
        if m:
            enrol_roots.append(m.group(1))
    return {
        "_provenance": provenance(
            "Every EnrolMart page whose HTML contains an <h2 "
            "class=\"product-price\"> tag — i.e. an actual product page. "
            "Prices are read from that tag verbatim.",
            ["data/crawl/index.jsonl", "data/raw/uspsonline.enrolmart.com/"],
            web_sources=enrol_roots),
        "products": products,
    }


# ---------------------------------------------------------------------------
# videos.json — America's Boating Channel program pages
# ---------------------------------------------------------------------------
# Capture everything up to the closing </script> tag — a lazy "first }" match
# truncates any JSON-LD that contains nested objects.
JSONLD = re.compile(
    r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
    re.DOTALL)
ISO_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def duration_seconds(iso):
    m = ISO_DURATION.fullmatch(iso or "")
    if not m:
        return None
    h, mins, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mins * 60 + s


def extract_videos(records):
    videos = []
    no_jsonld = 0
    for r in records:
        if (r["domain"] != "americasboatingchannel.uscreen.io"
                or not r.get("html_path")):
            continue
        url = r.get("normalized_url") or r["url"]
        if "/programs/" not in url:
            continue
        page = read_html(r)
        if not page:
            continue
        data = None
        for m in JSONLD.finditer(page):
            try:
                # strict=False: at least one channel page embeds a literal
                # newline inside a JSON string (invalid JSON, but real data).
                candidate = json.loads(m.group(1), strict=False)
            except json.JSONDecodeError:
                continue
            if candidate.get("@type") == "VideoObject":
                data = candidate
                break
        if not data:
            no_jsonld += 1
            continue
        videos.append({
            "name": data.get("name"),
            "url": url,
            "description": (data.get("description") or "")[:400] or None,
            "duration_seconds": duration_seconds(data.get("duration")),
            "upload_date": (data.get("uploadDate") or "")[:10] or None,
        })
    videos.sort(key=lambda v: (v["upload_date"] or "", v["name"] or ""))
    total_h = sum(v["duration_seconds"] or 0 for v in videos) / 3600
    log(f"  videos: {len(videos)} program pages with VideoObject JSON-LD "
        f"({no_jsonld} program pages without it were skipped); total "
        f"runtime {total_h:.1f} hours.")
    video_sources = list(
        _crawl.DOMAINS["americasboatingchannel.uscreen.io"]["seeds"])
    if videos:
        # One real program page as a clickable example of the per-video
        # sources (every video has its own /programs/<slug> page).
        video_sources.append(videos[0]["url"])
    return {
        "_provenance": provenance(
            "One record per video page on americasboatingchannel.uscreen.io "
            "(/programs/...), read from the schema.org VideoObject JSON-LD "
            "each page embeds. Pages lacking that block are counted and "
            "skipped, never guessed.",
            ["data/crawl/index.jsonl",
             "data/raw/americasboatingchannel.uscreen.io/"],
            web_sources=video_sources),
        "skipped_pages_without_jsonld": no_jsonld,
        "videos": videos,
    }


# ---------------------------------------------------------------------------
# link_graph.json
# ---------------------------------------------------------------------------
def extract_link_graph(records):
    cross = Counter()      # (from_domain, to_domain) for ABC properties
    external = Counter()   # external host -> link count
    for r in records:
        src = r["domain"]
        for link in r.get("links", []):
            host = re.sub(r"^https?://", "", link).split("/")[0].lower()
            dst = HOST_TO_DOMAIN.get(host)
            if dst and dst != src:
                cross[(src, dst)] += 1
            elif not dst:
                external[host] += 1

    # Local club websites (from the interactive club directory) get their
    # own list: they ARE the ABC ecosystem's long tail.
    clubs_path = os.path.join(PROJECT_ROOT, "data", "interactive",
                              "clubs.json")
    club_sites = []
    if os.path.exists(clubs_path):
        with open(clubs_path, encoding="utf-8") as fh:
            for club in json.load(fh)["clubs"]:
                if club.get("website"):
                    club_sites.append({"club": club["name"],
                                       "state": club.get("state"),
                                       "website": club["website"]})
    club_hosts = {re.sub(r"^https?://", "", c["website"]).split("/")[0].lower()
                  for c in club_sites}
    flagged = {h: n for h, n in external.items()
               if h in club_hosts}

    log(f"  link graph: {len(cross)} ABC-to-ABC link edges, "
        f"{len(external)} distinct external domains, "
        f"{len(club_sites)} local club websites (of which "
        f"{len(flagged)} also appear as links on crawled pages).")
    return {
        "_provenance": provenance(
            "Cross-links between the crawled ABC properties, plus every "
            "external domain those pages link to, counted from the outlink "
            "lists in the crawl index. Club websites come from the "
            "interactive club directory capture.",
            ["data/crawl/index.jsonl", "data/interactive/clubs.json"],
            web_sources=[f"https://{r['domain']}/" for r in records]),
        "abc_cross_links": [
            {"from": a, "to": b, "links": n}
            for (a, b), n in sorted(cross.items(), key=lambda kv: -kv[1])],
        "external_domains_top100": dict(external.most_common(100)),
        "external_domains_total": len(external),
        "club_websites": sorted(club_sites, key=lambda c: (c["state"] or "ZZ",
                                                           c["club"])),
    }


# ---------------------------------------------------------------------------
# content_dates.json — lastmod stamps from saved sitemap XML
# ---------------------------------------------------------------------------
SITEMAP_ENTRY = re.compile(
    r"<url>\s*<loc>\s*([^<]+?)\s*</loc>\s*"
    r"(?:<lastmod>\s*([^<]+?)\s*</lastmod>)?", re.DOTALL)


def extract_content_dates():
    entries = []
    sitemap_urls = []
    files = sorted(f for f in os.listdir(CRAWL_DIR)
                   if f.startswith("sitemap_") and f.endswith(".xml"))
    for fname in files:
        with open(os.path.join(CRAWL_DIR, fname), encoding="utf-8") as fh:
            xml = fh.read()
        domain = fname.split("_")[1]
        sitemap_urls.extend(
            _crawl.DOMAINS.get(domain, {}).get("sitemaps", []))
        n = 0
        for loc, lastmod in SITEMAP_ENTRY.findall(xml):
            if "<sitemapindex" in xml[:400]:
                continue
            entries.append({
                "url": html_mod.unescape(loc),
                "domain": domain,
                "lastmod": (lastmod or "")[:10] or None,
            })
            n += 1
        log(f"  content dates: {fname} contributed {n} URL entries.")
    if not files:
        log("  content dates: no saved sitemap XML found — the freshness "
            "timeline will be empty. Re-run scrape/crawl.py (it now saves "
            "every sitemap it reads).")
    return {
        "_provenance": provenance(
            "Per-URL <lastmod> stamps copied from the sitemap XML files the "
            "crawler saved. These are the sites' own statements of when "
            "each page last changed.",
            [os.path.join("data", "crawl", f) for f in files],
            web_sources=sitemap_urls),
        "entries": entries,
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    log(f"Reading crawl index from {INDEX_PATH} ...")
    records = load_index()
    log(f"{len(records)} unique URLs in the index. Extracting datasets:")

    outputs = {
        "pages.json": extract_pages(records),
        "education_catalog.json": extract_education(records),
        "enrolmart_products.json": extract_enrolmart(records),
        "videos.json": extract_videos(records),
        "link_graph.json": extract_link_graph(records),
        "content_dates.json": extract_content_dates(),
    }
    for fname, data in outputs.items():
        path = os.path.join(OUT_DIR, fname)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        log(f"Wrote {path}")
    log("Extraction finished. Every dataset was rebuilt from files on disk; "
        "re-running this script with the same inputs reproduces the same "
        "outputs.")


if __name__ == "__main__":
    main()
