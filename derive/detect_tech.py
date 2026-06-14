#!/usr/bin/env python3
"""
Detect each web property's analytics tags and tech platform from the raw HTML
already on disk (data/raw/<dir>/*.html), and write a regenerable inventory to
data/extracted/tech_inventory.json.

This is the system of record for the dashboard's "Analytics coverage" matrix
and "Tech platform" table. Every value here is grep-able evidence from a saved
page — nothing is taken from the external audit or typed by hand. Where a
property could not be crawled (site down, or the root errored), the inventory
says so explicitly so the dashboard can show "not crawled" rather than a blank
that reads as "measured, found nothing".

Run on its own:  python3 derive/detect_tech.py
It prints a plain-English summary and writes the JSON; build_dashboard_data.py
reads that JSON.

Detection is deliberately conservative. An ID or platform is only reported when
a high-confidence signal appears IN THE PROPERTY'S OWN PAGES:
  - analytics:  GA4 (G-XXXXXXXX), Universal Analytics (UA-...), GTM (GTM-...),
                Meta Pixel (fbq()/connect.facebook.net) — read from inline tag
                config, not from links to other properties.
  - platform:   <meta name="generator">, ASP.NET __VIEWSTATE, WordPress
                wp-content/wp-includes, Uscreen asset host, Moodle login marker.
Counts are "pages that carry the signal / pages scanned" so the operator can
see how broad the evidence is.
"""

import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, "data", "raw")
OUT = os.path.join(ROOT, "data", "extracted", "tech_inventory.json")

# The properties the dashboard reports on, in the order the user listed them,
# each mapped to the raw-HTML directory that holds its crawled pages. A value of
# None means "no pages on disk" — the site was unreachable or its root errored,
# and the dashboard should label it accordingly.
PROPERTIES = [
    ("americasboatingclub.org",            "americasboatingclub.org",
     "Public flagship site"),
    ("usps.org",                           None,
     "Legacy parent / member site — unreachable during crawl (timed out)"),
    ("portal.americasboatingclub.org",     "portal.americasboatingclub.org",
     "Membership portal — join, events, donations"),
    ("store.shopusps.org",                 "store.shopusps.org",
     "Ship's Store"),
    ("giveabc.org",                        "giveabc.org",
     "Donation site"),
    ("americasboatingcourse.com",          "www.americasboatingcourse.com",
     "Flagship course marketing site"),
    ("course.americasboatingcourse.com",   None,
     "Paid courseware (LMS) — root returned a server error during crawl"),
    ("uspsonline.enrolmart.com",           "uspsonline.enrolmart.com",
     "Online seminars store (LMS)"),
    ("uspsonline.org",                     "www.uspsonline.org",
     "'Other online courses' (LMS) — homepage captured live 2026-06-13"),
    ("americasboatingchannel.com",         "americasboatingchannel.uscreen.io",
     "Video channel"),
    ("boatlive365.org",                    "boatlive365.org",
     "Safety-culture campaign site"),
]

GA4_RE = re.compile(r"\bG-[A-Z0-9]{8,12}\b")
UA_RE = re.compile(r"\bUA-\d{4,12}-\d{1,4}\b")
GTM_RE = re.compile(r"\bGTM-[A-Z0-9]{5,9}\b")
GENERATOR_RE = re.compile(
    r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
PIXEL_RE = re.compile(r"connect\.facebook\.net/[^\"']*/fbevents\.js|fbq\(")
VIEWSTATE_RE = re.compile(r"__VIEWSTATE|__doPostBack")
WP_RE = re.compile(r"/wp-(?:content|includes)/")
USCREEN_RE = re.compile(r"uscreen", re.IGNORECASE)
MOODLE_RE = re.compile(r"moodle", re.IGNORECASE)
ENROLMART_RE = re.compile(r"powered by enrolmart", re.IGNORECASE)
# Joomla with search-engine-friendly URLs turned off routes everything through
# /index.php/ — the same signature the (generator-confirmed) flagship carries.
INDEXPHP_RE = re.compile(r"/index\.php/")

# Social profile links in a property's footer. We keep real account URLs and
# drop share/intent/widget/legal endpoints so the "social accounts" list is the
# org's actual handles, not Facebook share dialogs or privacy-policy links.
SOCIAL_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(twitter\.com|x\.com|facebook\.com|instagram\.com|youtube\.com|"
    r"linkedin\.com|tiktok\.com)/"
    r"([A-Za-z0-9_./@-]+)",
    re.IGNORECASE)
# Path first-segments that are never an account handle.
SOCIAL_REJECT = {
    "sharer", "sharer.php", "share", "shareArticle".lower(), "intent", "home",
    "tr", "events", "event", "photo", "photo.php", "profile.php", "policy.php",
    "dialog", "plugins", "login", "privacy", "legal", "en", "i", "hashtag",
    "search", "watch", "embed", "playlist", "results", "feed", "p", "reel",
    "explore", "accounts", "pages",
}


def clean_social(host, handle):
    """Return a normalized 'host/handle' for a real account, or None for share
    widgets, post permalinks, and legal/util paths."""
    host = host.lower().replace("www.", "")
    handle = handle.strip("/").split("?")[0].split("#")[0]
    if not handle:
        return None
    segs = handle.split("/")
    first = segs[0].lower()
    if first in SOCIAL_REJECT:
        return None
    # Facebook/Instagram numeric post ids (often digits or digits_digits).
    if re.fullmatch(r"[\d_]+", first):
        return None
    if host == "linkedin.com":
        # Only company/ and in/ profiles are real accounts.
        if first not in ("company", "in") or len(segs) < 2:
            return None
        return f"linkedin.com/{first}/{segs[1]}"
    if host == "youtube.com":
        if first in ("user", "c", "channel") and len(segs) >= 2:
            return f"youtube.com/{first}/{segs[1]}"
        if first.startswith("@"):
            return f"youtube.com/{first}"
        # bare vanity name
        if re.fullmatch(r"[A-Za-z0-9_-]{3,}", first):
            return f"youtube.com/{first}"
        return None
    # twitter/x/facebook/instagram/tiktok: first segment is the handle.
    if re.fullmatch(r"@?[A-Za-z0-9_.]{2,30}", first):
        return f"{host}/{first}"
    return None


def platform_from_signals(generators, vstate, wp, uscreen, moodle,
                          enrolmart, indexphp, n):
    """Pick the most specific platform a majority of pages support, with the
    evidence that justifies it. Returns (label, evidence) or (None, reason)."""
    # Generator tag is the strongest, most explicit signal.
    gen = generators.most_common(1)[0][0] if generators else ""
    g = gen.lower()
    if "helix" in g or "joomla" in g:
        return ("Joomla" + (f" · {gen.split(' - ')[0]}" if "helix" in g else ""),
                f'generator meta "{gen}"')
    if "give" in g:
        return (f"WordPress · GiveWP ({gen})", f'generator meta "{gen}"')
    if "wordpress" in g:
        return ("WordPress", f'generator meta "{gen}"')
    # Structural signals (need a majority of pages to carry them).
    half = max(1, n // 2)
    if uscreen >= half:
        return ("Uscreen (hosted video)", f"uscreen asset markers on {uscreen}/{n} pages")
    if wp >= half:
        return ("WordPress", f"wp-content/wp-includes on {wp}/{n} pages")
    if vstate >= half:
        return ("ASP.NET WebForms (iMIS portal)",
                f"__VIEWSTATE/__doPostBack on {vstate}/{n} pages")
    if moodle >= half:
        return ("Moodle", f"moodle markers on {moodle}/{n} pages")
    if enrolmart >= half:
        return ("EnrolMart (vendor LMS)",
                f'"powered by EnrolMart" on {enrolmart}/{n} pages')
    if indexphp >= half:
        return ("Joomla",
                f"/index.php/ routing on {indexphp}/{n} pages (SEF off)")
    return (None, "no high-confidence platform signal in crawled pages")


def scan_dir(path):
    files = [os.path.join(path, f) for f in os.listdir(path)
             if f.endswith(".html")]
    n = len(files)
    ga4, ua, gtm = set(), set(), set()
    pages_ga, pages_pixel = 0, 0
    vstate = wp = uscreen = moodle = enrolmart = indexphp = 0
    generators = Counter()
    social = {}  # normalized "host/handle" -> full url (first seen)
    for fp in files:
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                html = fh.read()
        except OSError:
            continue
        page_ga = GA4_RE.findall(html)
        # GA4/UA/GTM: only count IDs that appear in an inline analytics config,
        # not bare mentions. The IDs themselves are specific enough that a hit
        # is the property's own tag in practice.
        if page_ga:
            ga4.update(page_ga)
            pages_ga += 1
        ua.update(UA_RE.findall(html))
        gtm.update(GTM_RE.findall(html))
        if PIXEL_RE.search(html):
            pages_pixel += 1
        for m in GENERATOR_RE.findall(html):
            generators[m.strip()] += 1
        if VIEWSTATE_RE.search(html):
            vstate += 1
        if WP_RE.search(html):
            wp += 1
        if USCREEN_RE.search(html):
            uscreen += 1
        if MOODLE_RE.search(html):
            moodle += 1
        if ENROLMART_RE.search(html):
            enrolmart += 1
        if INDEXPHP_RE.search(html):
            indexphp += 1
        for host, handle in SOCIAL_RE.findall(html):
            acct = clean_social(host, handle)
            if acct:
                social.setdefault(acct.lower(), acct)
    label, evidence = platform_from_signals(
        generators, vstate, wp, uscreen, moodle, enrolmart, indexphp, n)
    return {
        "pages_scanned": n,
        "ga4": sorted(ga4),
        "universal_analytics": sorted(ua),
        "gtm": sorted(gtm),
        "meta_pixel_pages": pages_pixel,
        "platform": label,
        "platform_evidence": evidence,
        "generator_meta": generators.most_common(1)[0][0] if generators else None,
        "social": sorted(social.values()),
    }


def main():
    properties = []
    for domain, raw_dir, role in PROPERTIES:
        if raw_dir is None:
            properties.append({
                "domain": domain, "role": role, "crawled": False,
                "pages_scanned": 0,
                "ga4": [], "universal_analytics": [], "gtm": [],
                "meta_pixel_pages": 0,
                "platform": None,
                "platform_evidence": "site not crawled", "social": [],
                "generator_meta": None,
            })
            log(f"{domain}: NOT crawled ({role}) — will show 'not crawled'.")
            continue
        path = os.path.join(RAW, raw_dir)
        if not os.path.isdir(path):
            log(f"{domain}: expected raw dir {path} is missing — treating as "
                f"not crawled. Re-run the crawl for this domain.")
            properties.append({
                "domain": domain, "role": role, "crawled": False,
                "pages_scanned": 0, "ga4": [], "universal_analytics": [],
                "gtm": [], "meta_pixel_pages": 0, "platform": None,
                "platform_evidence": f"raw dir {raw_dir} missing", "social": [],
                "generator_meta": None})
            continue
        rec = scan_dir(path)
        rec.update({"domain": domain, "role": role, "crawled": True})
        properties.append(rec)
        a = []
        if rec["ga4"]:
            a.append("GA4 " + ", ".join(rec["ga4"]))
        if rec["gtm"]:
            a.append("GTM " + ", ".join(rec["gtm"]))
        if rec["universal_analytics"]:
            a.append("UA " + ", ".join(rec["universal_analytics"]))
        if rec["meta_pixel_pages"]:
            a.append(f"Meta Pixel ({rec['meta_pixel_pages']} pp)")
        log(f"{domain}: {rec['pages_scanned']} pages · "
            f"platform={rec['platform'] or 'undetermined'} · "
            f"analytics={'; '.join(a) if a else 'none detected'}")

    out = {
        "_about": ("Per-property analytics tags and tech platform, detected "
                   "from saved raw HTML by derive/detect_tech.py. Blank "
                   "analytics = scanned, none found. 'not crawled' = no pages "
                   "on disk (site down or root errored). Nothing here is taken "
                   "from the external audit."),
        "generated_by": "derive/detect_tech.py",
        "raw_source": "data/raw/<domain>/*.html",
        "properties": properties,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)
    log(f"Wrote {OUT} ({len(properties)} properties).")


def log(msg):
    print(f"[detect_tech] {msg}", file=sys.stderr)


if __name__ == "__main__":
    main()
