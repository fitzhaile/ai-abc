#!/usr/bin/env python3
"""
Fetch the Ship's Store catalog (store.shopusps.org) and extract every product.

Why this script exists, in plain English:
  The main crawler (scrape/crawl.py) uses Python's urllib, and the store's
  server lets those connections hang until they time out — while the exact
  same requests through curl or a real browser answer in under a second.
  So this script shells out to curl instead. If the store ever starts
  working with urllib again this script still works; it just exists to get
  around that server quirk.

What it does:
  - Walks the /Catalog/ category tree breadth-first (category pages and
    pagination only — product names, item codes, and prices all appear on
    the listing pages, so product detail pages are not fetched).
  - Honors the store's robots.txt Crawl-Delay of 20 seconds between requests.
  - Saves each page's raw HTML to data/raw/store.shopusps.org/ and appends
    a record to data/crawl/index.jsonl (same format as the main crawler).
  - Writes the merged product list to data/interactive/store_products.json.

Run it directly:  python3 scrape/fetch_store_catalog.py
Stdlib + the system curl binary; no third-party Python packages.
"""

import hashlib
import html as html_mod
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw", "store.shopusps.org")
CRAWL_DIR = os.path.join(PROJECT_ROOT, "data", "crawl")
INDEX_PATH = os.path.join(CRAWL_DIR, "index.jsonl")
OUT_PATH = os.path.join(PROJECT_ROOT, "data", "interactive",
                        "store_products.json")

BASE = "https://store.shopusps.org"
DELAY_SECONDS = 20  # the store's robots.txt asks for Crawl-Delay: 20
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

CATEGORY_LINK = re.compile(r'href="(/Catalog/[^"?#]*/)"')
PAGINATION_LINK = re.compile(r'href="(/Catalog/[^"]*[?&]page=\d+[^"]*)"')
PRODUCT_ANCHOR = re.compile(
    r'<a href="(?P<href>/Catalog/[^"]+\.html)">(?P<name>[^<]+)</a>')
ITEM_CODE = re.compile(r"Item Code:\s*([\w-]+)")
# The price appears in two markups across the store's category pages:
#   <span class="price">Price: $33.00</span>                      (plain)
#   <span class="price">Price: <span id=...>$64.00 – $73.00</span> (nested)
# Matching minimally up to the FIRST closing tag works for both.
PRICE_SPAN = re.compile(r'<span class="price">(?P<pricehtml>.*?)</span>',
                        re.DOTALL)
MONEY = re.compile(r"\$(\d[\d,]*(?:\.\d{2})?)")


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def curl_fetch(url):
    """Fetch a URL with curl; returns (status_code, body_text)."""
    result = subprocess.run(
        ["curl", "-s", "--max-time", "45", "-w", "\n%{http_code}",
         "-A", USER_AGENT, url],
        capture_output=True, text=True, timeout=60)
    body, _, status = result.stdout.rpartition("\n")
    return (int(status) if status.strip().isdigit() else 0), body


def parse_products(page_html, category_path):
    """Each product sits in its own <div class="info"> block; splitting on
    that marker keeps one product's fields from bleeding into the next."""
    products = []
    for chunk in page_html.split('<div class="info">')[1:]:
        anchor = PRODUCT_ANCHOR.search(chunk)
        if not anchor:
            continue  # an info div without a product link (empty category)
        code = ITEM_CODE.search(chunk)
        price_m = PRICE_SPAN.search(chunk)
        prices = [float(p.replace(",", ""))
                  for p in MONEY.findall(price_m.group("pricehtml"))] \
            if price_m else []
        products.append({
            "url": BASE + anchor.group("href"),
            "name": html_mod.unescape(anchor.group("name")).strip(),
            "item_code": code.group(1) if code else None,
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "listed_under": category_path,
        })
    return products


def reparse_from_disk():
    """Rebuild store_products.json from the raw HTML already on disk —
    no network requests. Used after a parser fix: the evidence is saved,
    so re-deriving must not require re-fetching."""
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pages = []
    with open(INDEX_PATH, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (r.get("domain") == "store.shopusps.org"
                    and r.get("html_path")
                    and "/Catalog/" in r.get("url", "")):
                pages.append(r)
    # Keep the newest record per URL (re-runs append).
    newest = {}
    for r in pages:
        newest[r["url"]] = r
    log(f"Re-parsing {len(newest)} saved Ship's Store catalog pages from "
        f"disk (no network requests).")
    merged = {}
    pages_report = []
    for url, r in sorted(newest.items()):
        path = "/" + url.split("/", 3)[3]
        try:
            with open(os.path.join(PROJECT_ROOT, r["html_path"]),
                      encoding="utf-8") as fh:
                body = fh.read()
        except OSError as exc:
            log(f"  {path}: raw HTML missing ({exc}) — run the fetch mode "
                f"to restore it.")
            pages_report.append({"path": path, "outcome": "raw HTML missing"})
            continue
        products = parse_products(body, path)
        for p in products:
            if p["url"] not in merged:
                merged[p["url"]] = {**p, "listed_under": [path]}
            else:
                ex = merged[p["url"]]
                if path not in ex["listed_under"]:
                    ex["listed_under"].append(path)
                for field in ("item_code", "price_min", "price_max"):
                    if ex[field] is None and p[field] is not None:
                        ex[field] = p[field]
        log(f"  {path}: {len(products)} products listed "
            f"(running unique total {len(merged)}).")
        pages_report.append({"path": path, "title": r.get("title"),
                             "products_listed": len(products),
                             "outcome": "re-parsed from saved HTML"})
    write_output(merged, pages_report, started,
                 mode="re-parsed from raw HTML saved by an earlier fetch run")


def write_output(merged, pages_report, started, mode):
    products = sorted(merged.values(), key=lambda p: p["name"])
    priced = sum(1 for p in products if p["price_min"] is not None)
    log(f"Done: {len(products)} unique products across "
        f"{len(pages_report)} catalog pages; {priced} have prices "
        f"({len(products) - priced} without).")
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump({
            "_provenance": {
                "description": (
                    "Every product listed in the Ship's Store catalog "
                    "(store.shopusps.org, a NetSuite SiteBuilder store), "
                    "harvested from the category listing pages which show "
                    "each product's name, item code and price."),
                "source_url": BASE + "/Catalog/",
                "script": "scrape/fetch_store_catalog.py",
                "mode": mode,
                "fetched_at": started,
                "crawl_delay_honored_seconds": DELAY_SECONDS,
                "pages_visited": pages_report,
                "unique_products": len(products),
            },
            "products": products,
        }, fh, indent=1, ensure_ascii=False)
    log(f"Product catalog written to {OUT_PATH}")


def main():
    if "--reparse" in sys.argv:
        reparse_from_disk()
        return
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(CRAWL_DIR, exist_ok=True)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index_fh = open(INDEX_PATH, "a", encoding="utf-8")

    to_visit = ["/Catalog/"]
    seen_pages = set(to_visit)
    merged = {}
    pages_report = []

    log(f"Walking the Ship's Store catalog with curl, one request every "
        f"{DELAY_SECONDS}s as its robots.txt requests. Listing pages only — "
        f"names, item codes and prices are all on the listings.")

    while to_visit:
        path = to_visit.pop(0)
        url = BASE + path
        try:
            status, body = curl_fetch(url)
        except Exception as exc:
            log(f"  {path}: curl failed ({exc.__class__.__name__}: {exc}) — "
                f"skipping this page; re-run to retry.")
            pages_report.append({"path": path, "outcome": f"failed: {exc}"})
            time.sleep(DELAY_SECONDS)
            continue
        if status != 200:
            log(f"  {path}: HTTP {status} — skipping.")
            pages_report.append({"path": path, "outcome": f"HTTP {status}"})
            time.sleep(DELAY_SECONDS)
            continue

        digest = hashlib.sha1(url.encode()).hexdigest()[:16]
        rel_path = os.path.join("data", "raw", "store.shopusps.org",
                                f"{digest}.html")
        with open(os.path.join(PROJECT_ROOT, rel_path), "w",
                  encoding="utf-8") as fh:
            fh.write(body)

        title_m = re.search(r"<title>([^<]*)</title>", body)
        products = parse_products(body, path)
        new = 0
        for p in products:
            if p["url"] not in merged:
                merged[p["url"]] = {**p, "listed_under": [path]}
                new += 1
            else:
                ex = merged[p["url"]]
                if path not in ex["listed_under"]:
                    ex["listed_under"].append(path)
                for field in ("item_code", "price_min", "price_max"):
                    if ex[field] is None and p[field] is not None:
                        ex[field] = p[field]

        cats = set(CATEGORY_LINK.findall(body))
        pages = set(html_mod.unescape(u) for u in PAGINATION_LINK.findall(body))
        added = 0
        for link in sorted(cats | pages):
            if link not in seen_pages:
                seen_pages.add(link)
                to_visit.append(link)
                added += 1

        log(f"  {path}: {len(products)} products listed ({new} new, total "
            f"{len(merged)}); {added} new category/page links queued, "
            f"{len(to_visit)} to go.")
        pages_report.append({"path": path,
                             "title": title_m.group(1) if title_m else None,
                             "products_listed": len(products),
                             "outcome": "fetched ok"})
        index_fh.write(json.dumps({
            "url": url, "normalized_url": url.lower(),
            "domain": "store.shopusps.org", "depth": path.count("/") - 2,
            "discovered_from": "fetch_store_catalog.py category walk",
            "fetched_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "final_url": url, "http_status": status,
            "content_type": "text/html", "bytes": len(body),
            "title": title_m.group(1) if title_m else None,
            "html_path": rel_path,
            "outcome": "fetched ok (via curl — the store's server hangs "
                       "Python urllib connections, see "
                       "scrape/fetch_store_catalog.py)",
        }, ensure_ascii=False) + "\n")
        index_fh.flush()
        time.sleep(DELAY_SECONDS)

    write_output(merged, pages_report, started,
                 mode="fetched live with curl, one request per "
                      f"{DELAY_SECONDS}s")


if __name__ == "__main__":
    main()
