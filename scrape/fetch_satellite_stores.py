#!/usr/bin/env python3
"""
Capture the two ABC-branded storefronts hosted on vendor domains, which the
Ship's Store links to from its homepage:

  1. abcvesselexaminer.brandingbygeiger.com — vessel examiner gear store.
     As of mid-May 2026 this site shows a "Closed" notice; this script
     records whatever status the page currently reports.
  2. cpdean.com/collections/awards — the "America's Boating Club" awards
     collection on C.P. Dean's Shopify store. Shopify exposes a public
     products.json for every collection (and C.P. Dean's robots.txt
     explicitly says public product data is crawlable), so the full
     collection is fetched as JSON.

Output: data/interactive/satellite_stores.json
Run it directly:  python3 scrape/fetch_satellite_stores.py
Stdlib + the system curl binary.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(PROJECT_ROOT, "data", "interactive",
                        "satellite_stores.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
GEIGER_URL = "https://abcvesselexaminer.brandingbygeiger.com/"
CPDEAN_URL = "https://cpdean.com/collections/awards/products.json?limit=250"


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def curl(url):
    result = subprocess.run(
        ["curl", "-sL", "--max-time", "30", "-A", USER_AGENT, url],
        capture_output=True, text=True, timeout=45)
    return result.stdout


def main():
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    log(f"Checking the Geiger vessel-examiner store at {GEIGER_URL} ...")
    geiger_html = curl(GEIGER_URL)
    title = re.search(r"<title>([^<]*)</title>", geiger_html)
    title = title.group(1).strip() if title else None
    # The closure date may be wrapped in markup, e.g.
    #   <h2>Closed as of <span class="date">05/18/2026</span></h2>
    closed = re.search(r"Closed\s+as\s+of\s*(?:<[^>]+>\s*)*([\d/]+)",
                       geiger_html)
    headings = [re.sub(r"<[^>]+>", "", h).strip()
                for h in re.findall(r"<h[12][^>]*>(.*?)</h[12]>",
                                    geiger_html, re.DOTALL)]
    geiger = {
        "url": GEIGER_URL,
        "page_title": title,
        "status": (f"closed as of {closed.group(1)}" if closed
                   else "open or unknown — no closure notice found"),
        "headings": [h.strip() for h in headings],
    }
    log(f"  -> page title: {title!r}; status: {geiger['status']}")

    log(f"Fetching the C.P. Dean ABC awards collection from {CPDEAN_URL} ...")
    cpdean_raw = json.loads(curl(CPDEAN_URL))
    products = []
    for p in cpdean_raw.get("products", []):
        prices = [float(v["price"]) for v in p.get("variants", [])
                  if v.get("price")]
        products.append({
            "title": p.get("title"),
            "handle": p.get("handle"),
            "url": f"https://cpdean.com/products/{p.get('handle')}",
            "product_type": p.get("product_type") or None,
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "n_variants": len(p.get("variants", [])),
        })
    log(f"  -> {len(products)} products in the collection.")

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump({
            "_provenance": {
                "description": (
                    "ABC-branded storefronts on vendor domains, linked from "
                    "the Ship's Store homepage. C.P. Dean product data comes "
                    "from Shopify's public collection products.json, which "
                    "cpdean.com's robots.txt explicitly marks as crawlable."),
                "script": "scrape/fetch_satellite_stores.py",
                "fetched_at": fetched_at,
                "sources": [GEIGER_URL, CPDEAN_URL],
            },
            "geiger_vessel_examiner_store": geiger,
            "cpdean_awards_collection": {
                "source_url": CPDEAN_URL,
                "products": products,
            },
        }, fh, indent=1, ensure_ascii=False)
    log(f"Satellite store data written to {OUT_PATH}")


if __name__ == "__main__":
    main()
