#!/usr/bin/env python3
"""
Polite multi-domain crawler for the America's Boating Club web ecosystem.

What it does, in plain English:
  - Crawls americasboatingclub.org plus the related ABC sites discovered on it
    (ship's store, enrollment store, parent org usps.org, video channel, etc.).
  - Seeds each domain from its XML sitemap when one exists, then breadth-first
    follows in-scope links.
  - Obeys each domain's robots.txt (including Crawl-Delay — the ship's store
    asks for 20 seconds between requests, so that domain crawls slowly).
  - Saves raw HTML to data/raw/<domain>/<hash>.html and appends one JSON record
    per URL to data/crawl/index.jsonl explaining what happened and why.
  - Logs every decision (fetched / skipped / failed / capped) in plain English
    to data/crawl/crawl.log and stdout.
  - If stopped and re-run, it resumes: URLs already in index.jsonl are not
    fetched again.

Run it directly:            python3 scrape/crawl.py
Crawl only some domains:    python3 scrape/crawl.py --domains store.shopusps.org,giveabc.org
Smoke test (5 pages each):  python3 scrape/crawl.py --max-pages 5

Stdlib only — no third-party dependencies.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Paths (everything lives under the project root, relative to this file)
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
CRAWL_DIR = os.path.join(PROJECT_ROOT, "data", "crawl")
INDEX_PATH = os.path.join(CRAWL_DIR, "index.jsonl")
LOG_PATH = os.path.join(CRAWL_DIR, "crawl.log")
SUMMARY_PATH = os.path.join(CRAWL_DIR, "summary.json")
EXTERNAL_DOMAINS_PATH = os.path.join(CRAWL_DIR, "external_domains.json")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Domain configuration.
# Each entry is one crawl "scope". `hosts` lists every hostname that belongs
# to the scope (redirect aliases included). `delay` is the minimum seconds
# between requests; if robots.txt asks for more, robots.txt wins.
# Caps exist so a runaway link space can't crawl forever — when a cap is hit
# the crawler SAYS SO and dumps the unfetched queue, it never hides it.
# ---------------------------------------------------------------------------
DOMAINS = {
    "americasboatingclub.org": {
        "hosts": ["americasboatingclub.org", "www.americasboatingclub.org"],
        "seeds": ["https://americasboatingclub.org/"],
        "sitemaps": [
            "https://americasboatingclub.org/index.php?option=com_jmap&view=sitemap&format=xml"
        ],
        "delay": 0.8,
        "max_pages": 450,
        "max_depth": 4,
    },
    "www.usps.org": {
        "hosts": ["www.usps.org", "usps.org"],
        "seeds": ["https://www.usps.org/"],
        "sitemaps": [],
        "delay": 1.2,
        "max_pages": 250,
        "max_depth": 3,
    },
    "www.americasboatingcourse.com": {
        "hosts": [
            "www.americasboatingcourse.com",
            "americasboatingcourse.com",
            "course.americasboatingcourse.com",
        ],
        "seeds": ["https://www.americasboatingcourse.com/"],
        "sitemaps": ["https://www.americasboatingcourse.com/sitemap.xml"],
        "delay": 0.8,
        "max_pages": 120,
        "max_depth": 3,
    },
    "americasboatingchannel.uscreen.io": {
        "hosts": [
            "americasboatingchannel.uscreen.io",
            "americasboatingchannel.com",
            "www.americasboatingchannel.com",
        ],
        "seeds": ["https://americasboatingchannel.uscreen.io/catalog"],
        "sitemaps": ["https://americasboatingchannel.uscreen.io/sitemap.xml"],
        "delay": 0.8,
        # The Uscreen sitemap lists ~1,250 URLs (one per video program); the
        # cap is set above that so every video page can be fetched.
        "max_pages": 1300,
        "max_depth": 3,
    },
    "boatlive365.org": {
        "hosts": ["boatlive365.org", "www.boatlive365.org"],
        "seeds": ["https://boatlive365.org/"],
        "sitemaps": ["https://boatlive365.org/sitemap_index.xml"],
        "delay": 0.8,
        "max_pages": 180,
        "max_depth": 3,
    },
    "giveabc.org": {
        "hosts": ["giveabc.org", "www.giveabc.org"],
        "seeds": ["https://giveabc.org/"],
        "sitemaps": ["https://giveabc.org/sitemap.xml"],
        "delay": 0.8,
        "max_pages": 60,
        "max_depth": 3,
    },
    "uspsonline.enrolmart.com": {
        "hosts": ["uspsonline.enrolmart.com"],
        "seeds": ["https://uspsonline.enrolmart.com/"],
        "sitemaps": ["https://uspsonline.enrolmart.com/sitemap.xml"],
        "delay": 1.0,
        "max_pages": 300,
        "max_depth": 4,
    },
    # The ship's store robots.txt sets "Crawl-Delay: 20" — we honor it, which
    # makes this domain slow on purpose. The page cap keeps total time sane;
    # anything left unfetched is reported, not silently dropped.
    "store.shopusps.org": {
        "hosts": ["store.shopusps.org", "shopusps.org", "www.shopusps.org"],
        "seeds": ["https://store.shopusps.org/"],
        "sitemaps": ["https://store.shopusps.org/sitemap.xml"],
        "delay": 20.0,
        "max_pages": 80,
        "max_depth": 3,
    },
    # The member portal is login-gated (every URL redirects to a login page).
    # We record the login gate itself so the inventory can say so, and stop.
    "portal.americasboatingclub.org": {
        "hosts": ["portal.americasboatingclub.org"],
        "seeds": ["https://portal.americasboatingclub.org/"],
        "sitemaps": [],
        "delay": 1.0,
        "max_pages": 25,
        "max_depth": 2,
    },
    # Legacy file host; only fetch what other ABC pages link to directly.
    "www.uspsonline.org": {
        "hosts": ["www.uspsonline.org", "uspsonline.org"],
        "seeds": [],
        "sitemaps": [],
        "delay": 1.0,
        "max_pages": 40,
        "max_depth": 2,
    },
}

HOST_TO_DOMAIN = {}
for _key, _cfg in DOMAINS.items():
    for _h in _cfg["hosts"]:
        HOST_TO_DOMAIN[_h] = _key

# File extensions we never download as pages.
SKIP_EXTENSIONS = re.compile(
    r"\.(jpe?g|png|gif|svg|webp|ico|bmp|tiff?|css|js|mjs|json|xml|txt|"
    r"zip|gz|tgz|rar|7z|exe|dmg|msi|"
    r"docx?|xlsx?|pptx?|csv|"
    r"mp3|mp4|m4a|m4v|mov|avi|wmv|webm|"
    r"woff2?|ttf|eot|otf|ics)(\?|$)",
    re.IGNORECASE,
)
# PDFs are recorded (they're real content) but not downloaded.
PDF_EXTENSION = re.compile(r"\.pdf(\?|$)", re.IGNORECASE)

# URL substrings that mean "do not crawl" (auth flows, carts, admin, print
# views, session traps, calendar/feed parameter explosions).
SKIP_SUBSTRINGS = [
    "logout", "/login", "login.aspx", "formsauthentication", "sign_in",
    "sign_up", "/users/password", "/administrator/", "/cache/", "/cart",
    "add-to-cart", "/checkout", "print=1", "tmpl=component", "format=pdf",
    "task=", "phpsessid", "sessionid", "returnurl", "/wp-admin", "/wp-json",
    "attachment_id", "replytocom", "/feed/", "format=feed", "/component/users",
    "/searchresults", "searchterm=",
]

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                   "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid"}

MAX_LINKS_PER_PAGE = 600          # outlinks stored per page record
MAX_WALL_SECONDS = 80 * 60        # absolute safety stop for the whole run
REQUEST_TIMEOUT = 35

stop_requested = threading.Event()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LinkAndTitleParser(HTMLParser):
    """Pulls <a href> links and the <title> out of an HTML page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.title_parts = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value.strip())
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)

    @property
    def title(self):
        return re.sub(r"\s+", " ", "".join(self.title_parts)).strip()[:300]


def normalize_url(url):
    """Canonical form so the same page isn't fetched twice.
    Lowercases host, strips fragments and tracking params, sorts the query."""
    parsed = urllib.parse.urlsplit(url)
    scheme = "https" if parsed.scheme in ("http", "https") else parsed.scheme
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    query_pairs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query = urllib.parse.urlencode(sorted(query_pairs))
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urllib.parse.urlunsplit((scheme, host, path, query, ""))


def url_domain_key(url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    return HOST_TO_DOMAIN.get(host)


def should_skip(url):
    """Returns a plain-English reason to skip this URL, or None to proceed."""
    lower = url.lower()
    if SKIP_EXTENSIONS.search(lower):
        return "binary/static file type"
    for sub in SKIP_SUBSTRINGS:
        if sub in lower:
            return f"URL matches do-not-crawl pattern '{sub}'"
    if len(url) > 500:
        return "URL longer than 500 chars (likely a parameter trap)"
    return None


def fetch_bytes(url, timeout=REQUEST_TIMEOUT):
    """GET a URL, transparently handling gzip. Returns (final_url, status,
    content_type, body_bytes)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(8 * 1024 * 1024)  # 8 MB hard limit per page
        if resp.headers.get("Content-Encoding", "") == "gzip":
            try:
                body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
            except OSError:
                pass
        content_type = resp.headers.get("Content-Type", "")
        return resp.geturl(), resp.status, content_type, body


def decode_html(body, content_type):
    match = re.search(r"charset=([\w-]+)", content_type or "")
    encodings = [match.group(1)] if match else []
    encodings += ["utf-8", "latin-1"]
    for enc in encodings:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------
class Crawler:
    def __init__(self, domain_filter=None, max_pages_override=None):
        self.lock = threading.Lock()
        self.visited = set()        # normalized URLs already handled this/any run
        self.records_written = 0
        self.external_domains = {}  # host -> count of links seen pointing there
        self.started_at = time.time()
        self.domain_filter = domain_filter
        self.max_pages_override = max_pages_override
        self.stats = {k: {"fetched": 0, "skipped": 0, "failed": 0, "capped": 0}
                      for k in DOMAINS}
        # Cross-domain handoff: links found on domain A that belong to domain B.
        self.pending_external = {k: [] for k in DOMAINS}

        os.makedirs(RAW_DIR, exist_ok=True)
        os.makedirs(CRAWL_DIR, exist_ok=True)
        self.log_fh = open(LOG_PATH, "a", encoding="utf-8")
        self.index_fh = open(INDEX_PATH, "a", encoding="utf-8")
        self._load_previous_run()

    # ---------------- logging / persistence ----------------
    def log(self, message):
        line = f"[{now_iso()}] {message}"
        with self.lock:
            print(line, flush=True)
            self.log_fh.write(line + "\n")
            self.log_fh.flush()

    def write_record(self, record):
        with self.lock:
            self.index_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.index_fh.flush()
            self.records_written += 1

    def _load_previous_run(self):
        if not os.path.exists(INDEX_PATH):
            return
        count = 0
        with open(INDEX_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.visited.add(rec.get("normalized_url", ""))
                count += 1
        if count:
            self.log(f"Resuming: found {count} URLs already handled in a "
                     f"previous run; they will not be fetched again.")

    # ---------------- robots ----------------
    def load_robots(self, domain_key, cfg):
        """Fetch robots.txt for the domain's primary host. If it can't be
        fetched, we stay conservative: crawl is allowed but we keep our own
        delay and skip patterns, and we say so in the log."""
        primary = cfg["hosts"][0]
        robots_url = f"https://{primary}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        try:
            final_url, status, ctype, body = fetch_bytes(robots_url, timeout=20)
            if status == 200 and b"<html" not in body[:200].lower():
                rp.parse(decode_html(body, ctype).splitlines())
                crawl_delay = rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*")
                effective = max(cfg["delay"], float(crawl_delay or 0))
                if crawl_delay:
                    self.log(f"{domain_key}: robots.txt asks for a "
                             f"{crawl_delay}s crawl delay — using "
                             f"{effective}s between requests.")
                return rp, effective
            self.log(f"{domain_key}: robots.txt returned HTTP {status} or "
                     f"HTML (not a robots file) — treating as 'no rules', "
                     f"keeping our own {cfg['delay']}s delay and skip list.")
        except Exception as exc:
            self.log(f"{domain_key}: could not fetch robots.txt "
                     f"({exc.__class__.__name__}: {exc}) — treating as "
                     f"'no rules', keeping our own delay and skip list.")
        rp.parse([])  # empty rules => everything allowed
        return rp, cfg["delay"]

    # ---------------- sitemaps ----------------
    def fetch_sitemap_urls(self, domain_key, sitemap_url, seen=None):
        """Returns page URLs from a sitemap, following one level of
        sitemap-index nesting."""
        seen = seen if seen is not None else set()
        if sitemap_url in seen or len(seen) > 20:
            return []
        seen.add(sitemap_url)
        try:
            final_url, status, ctype, body = fetch_bytes(sitemap_url, timeout=30)
        except Exception as exc:
            self.log(f"{domain_key}: sitemap {sitemap_url} could not be "
                     f"fetched ({exc.__class__.__name__}) — relying on "
                     f"link-following instead.")
            return []
        if status != 200:
            self.log(f"{domain_key}: sitemap {sitemap_url} returned HTTP "
                     f"{status} — relying on link-following instead.")
            return []
        text = decode_html(body, ctype)
        if "<html" in text[:300].lower():
            self.log(f"{domain_key}: {sitemap_url} returned an HTML page, "
                     f"not XML — this domain has no usable sitemap.")
            return []
        # Keep the sitemap XML itself: its <lastmod> stamps are the only
        # per-page modification dates many of these sites expose.
        fname = (f"sitemap_{domain_key}_"
                 f"{hashlib.sha1(sitemap_url.encode()).hexdigest()[:8]}.xml")
        with open(os.path.join(CRAWL_DIR, fname), "w",
                  encoding="utf-8") as fh:
            fh.write(text)
        locs = [
            urllib.parse.unquote(m.replace("&amp;", "&")).strip()
            for m in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text)
        ]
        if "<sitemapindex" in text[:600]:
            self.log(f"{domain_key}: {sitemap_url} is a sitemap INDEX with "
                     f"{len(locs)} sub-sitemaps; reading each one.")
            urls = []
            for sub in locs:
                urls.extend(self.fetch_sitemap_urls(domain_key, sub, seen))
            return urls
        self.log(f"{domain_key}: sitemap {sitemap_url} lists {len(locs)} URLs.")
        return locs

    # ---------------- per-domain worker ----------------
    def crawl_domain(self, domain_key):
        cfg = DOMAINS[domain_key]
        max_pages = self.max_pages_override or cfg["max_pages"]
        rp, delay = self.load_robots(domain_key, cfg)

        queue = []      # (depth, url, discovered_from)
        queued = set()  # normalized urls in queue or processed by this worker

        def enqueue(url, depth, source):
            norm = normalize_url(url)
            with self.lock:
                if norm in self.visited:
                    return
            if norm in queued:
                return
            queued.add(norm)
            queue.append((depth, url, source))

        for sm in cfg["sitemaps"]:
            for u in self.fetch_sitemap_urls(domain_key, sm):
                if url_domain_key(u) == domain_key:
                    enqueue(u, 0, f"sitemap:{sm}")
        for seed in cfg["seeds"]:
            enqueue(seed, 0, "seed")

        # If an earlier run hit this domain's page cap, it dumped the URLs it
        # never got to. Pick those up as seeds now (already-fetched ones are
        # skipped by the visited check), then set the file aside so it can't
        # be mistaken for a fresh report.
        unfetched_path = os.path.join(CRAWL_DIR,
                                      f"unfetched_{domain_key}.json")
        if os.path.exists(unfetched_path):
            with open(unfetched_path, encoding="utf-8") as fh:
                leftovers = json.load(fh).get("urls", [])
            for item in leftovers:
                enqueue(item["url"], item.get("depth", 0),
                        "unfetched queue from a previous run")
            os.rename(unfetched_path, unfetched_path + ".consumed")
            self.log(f"{domain_key}: picked up {len(leftovers)} URLs left "
                     f"unfetched by a previous run (file renamed to "
                     f"*.consumed).")

        fetched = 0
        while queue:
            if stop_requested.is_set():
                self.log(f"{domain_key}: stop requested — leaving "
                         f"{len(queue)} queued URLs unfetched (saved to "
                         f"unfetched_{domain_key}.json).")
                self.dump_unfetched(domain_key, queue)
                return
            if time.time() - self.started_at > MAX_WALL_SECONDS:
                self.log(f"{domain_key}: overall time limit "
                         f"({MAX_WALL_SECONDS // 60} min) reached — leaving "
                         f"{len(queue)} queued URLs unfetched (saved to "
                         f"unfetched_{domain_key}.json).")
                self.dump_unfetched(domain_key, queue)
                return
            if fetched >= max_pages:
                self.stats[domain_key]["capped"] = len(queue)
                self.log(f"{domain_key}: page cap of {max_pages} reached — "
                         f"{len(queue)} discovered URLs were NOT fetched. "
                         f"They are listed in unfetched_{domain_key}.json; "
                         f"raise max_pages for this domain and re-run to "
                         f"get them (already-fetched pages are skipped on "
                         f"resume).")
                self.dump_unfetched(domain_key, queue)
                return

            depth, url, source = queue.pop(0)
            norm = normalize_url(url)
            with self.lock:
                if norm in self.visited:
                    continue
                self.visited.add(norm)

            skip_reason = should_skip(url)
            if skip_reason:
                self.stats[domain_key]["skipped"] += 1
                self.write_record({
                    "url": url, "normalized_url": norm, "domain": domain_key,
                    "depth": depth, "discovered_from": source,
                    "outcome": f"skipped — {skip_reason}",
                    "fetched_at": now_iso(),
                })
                continue
            if PDF_EXTENSION.search(url.lower()):
                self.stats[domain_key]["skipped"] += 1
                self.write_record({
                    "url": url, "normalized_url": norm, "domain": domain_key,
                    "depth": depth, "discovered_from": source,
                    "outcome": "recorded link only — PDF document, body not "
                               "downloaded",
                    "is_pdf": True, "fetched_at": now_iso(),
                })
                continue
            path_and_query = url[url.find("/", 8):] if "/" in url[8:] else "/"
            if not rp.can_fetch(USER_AGENT, url) and not rp.can_fetch("*", url):
                self.stats[domain_key]["skipped"] += 1
                self.write_record({
                    "url": url, "normalized_url": norm, "domain": domain_key,
                    "depth": depth, "discovered_from": source,
                    "outcome": f"skipped — robots.txt disallows "
                               f"{path_and_query}",
                    "fetched_at": now_iso(),
                })
                continue

            # Polite delay before every request to this domain.
            time.sleep(delay)
            record = {
                "url": url, "normalized_url": norm, "domain": domain_key,
                "depth": depth, "discovered_from": source,
                "fetched_at": now_iso(),
            }
            try:
                final_url, status, ctype, body = self.fetch_with_retry(
                    domain_key, url, delay)
            except Exception as exc:
                self.stats[domain_key]["failed"] += 1
                record["outcome"] = (
                    f"failed — {exc.__class__.__name__}: {exc}. "
                    f"Next check: open the URL in a browser; if it loads, "
                    f"the server may be blocking automated clients.")
                self.write_record(record)
                self.log(f"{domain_key}: FAILED {url} "
                         f"({exc.__class__.__name__}: {exc})")
                continue

            record["final_url"] = final_url
            record["http_status"] = status
            record["content_type"] = (ctype or "").split(";")[0]
            record["bytes"] = len(body)
            fetched += 1
            self.stats[domain_key]["fetched"] += 1

            final_norm = normalize_url(final_url)
            if final_norm != norm:
                with self.lock:
                    self.visited.add(final_norm)
            final_domain = url_domain_key(final_url)
            if final_domain is None:
                host = urllib.parse.urlsplit(final_url).netloc.lower()
                record["outcome"] = (f"fetched but redirected OUT of ABC scope "
                                     f"to {host} — body not saved")
                self.write_record(record)
                self.note_external(host)
                continue

            if "html" not in (ctype or "").lower():
                record["outcome"] = (f"fetched but content-type is "
                                     f"'{record['content_type']}', not HTML — "
                                     f"body not saved")
                self.write_record(record)
                continue

            html = decode_html(body, ctype)
            parser = LinkAndTitleParser()
            try:
                parser.feed(html)
            except Exception:
                pass  # title/links best-effort; raw HTML is still saved
            record["title"] = parser.title

            digest = hashlib.sha1(final_norm.encode()).hexdigest()[:16]
            rel_path = os.path.join("data", "raw", domain_key,
                                    f"{digest}.html")
            abs_path = os.path.join(PROJECT_ROOT, rel_path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as fh:
                fh.write(html)
            record["html_path"] = rel_path

            # Resolve + bucket links: same-domain → queue; other ABC domain →
            # hand off; anything else → external tally.
            outlinks = []
            for href in parser.links[:MAX_LINKS_PER_PAGE * 2]:
                if href.startswith(("mailto:", "tel:", "javascript:", "#",
                                    "data:")):
                    continue
                absolute = urllib.parse.urljoin(final_url, href)
                if not absolute.startswith(("http://", "https://")):
                    continue
                outlinks.append(absolute)
            record["links"] = outlinks[:MAX_LINKS_PER_PAGE]
            record["n_links"] = len(outlinks)
            record["outcome"] = "fetched ok"
            self.write_record(record)

            for absolute in outlinks:
                link_domain = url_domain_key(absolute)
                if link_domain == domain_key:
                    if depth + 1 <= cfg["max_depth"]:
                        enqueue(absolute, depth + 1, final_url)
                elif link_domain is not None:
                    with self.lock:
                        self.pending_external[link_domain].append(
                            (absolute, final_url))
                else:
                    self.note_external(
                        urllib.parse.urlsplit(absolute).netloc.lower())

            if fetched % 25 == 0:
                self.log(f"{domain_key}: progress — {fetched} pages fetched, "
                         f"{len(queue)} queued.")

        self.log(f"{domain_key}: queue exhausted after {fetched} pages — "
                 f"every discovered in-scope URL was handled.")

    def fetch_with_retry(self, domain_key, url, delay):
        last_exc = None
        for attempt in (1, 2):
            try:
                return fetch_bytes(url)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 406, 429, 500, 502, 503) and attempt == 1:
                    self.log(f"{domain_key}: HTTP {exc.code} on {url} — "
                             f"waiting {max(10, delay * 3):.0f}s and retrying "
                             f"once.")
                    time.sleep(max(10, delay * 3))
                    last_exc = exc
                    continue
                raise
            except Exception as exc:
                if attempt == 1:
                    self.log(f"{domain_key}: {exc.__class__.__name__} on "
                             f"{url} — retrying once in 10s.")
                    time.sleep(10)
                    last_exc = exc
                    continue
                raise
        raise last_exc

    def note_external(self, host):
        if not host:
            return
        with self.lock:
            self.external_domains[host] = self.external_domains.get(host, 0) + 1

    def dump_unfetched(self, domain_key, queue):
        path = os.path.join(CRAWL_DIR, f"unfetched_{domain_key}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "explanation": (
                        "URLs discovered for this domain but not fetched "
                        "because the page cap or time limit was reached. "
                        "Raise max_pages in scrape/crawl.py and re-run; "
                        "already-fetched pages are skipped automatically."),
                    "count": len(queue),
                    "urls": [{"url": u, "depth": d, "discovered_from": s}
                             for d, u, s in queue],
                },
                fh, indent=2)

    # ---------------- orchestration ----------------
    def run(self):
        keys = [k for k in DOMAINS
                if not self.domain_filter or k in self.domain_filter]
        self.log(f"Crawl starting for {len(keys)} domains: {', '.join(keys)}")
        threads = []
        for key in keys:
            t = threading.Thread(target=self.crawl_domain_safe, args=(key,),
                                 name=key)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        # Second pass: URLs that belong to a domain but were discovered on a
        # different domain after that domain's worker already finished
        # (e.g. uspsonline.org file links found on usps.org pages).
        handoff = []
        for key in keys:
            extras = self.pending_external.get(key, [])
            fresh = []
            seen = set()
            for url, src in extras:
                norm = normalize_url(url)
                if norm in self.visited or norm in seen:
                    continue
                seen.add(norm)
                fresh.append((url, src))
            if fresh:
                handoff.append((key, fresh))
        for key, fresh in handoff:
            self.log(f"{key}: second pass — {len(fresh)} URLs for this domain "
                     f"were discovered on OTHER ABC domains; fetching them "
                     f"now (same caps and delays apply).")
            cfg = DOMAINS[key]
            for url, src in fresh:
                cfg.setdefault("seeds", [])
            # Re-run the domain worker with these as seeds via a small queue:
            self._second_pass(key, fresh)

        self.finish()

    def _second_pass(self, domain_key, url_source_pairs):
        cfg = DOMAINS[domain_key]
        rp, delay = self.load_robots(domain_key, cfg)
        budget = max(0, (self.max_pages_override or cfg["max_pages"])
                     - self.stats[domain_key]["fetched"])
        if budget == 0:
            self.log(f"{domain_key}: second pass skipped — page cap already "
                     f"used up. The {len(url_source_pairs)} cross-domain URLs "
                     f"are listed in unfetched_{domain_key}.json.")
            self.dump_unfetched(domain_key,
                                [(0, u, s) for u, s in url_source_pairs])
            return
        queue = [(0, u, s) for u, s in url_source_pairs[:budget]]
        leftover = url_source_pairs[budget:]
        if leftover:
            self.log(f"{domain_key}: second pass can only fetch {budget} of "
                     f"{len(url_source_pairs)} cross-domain URLs before the "
                     f"page cap; the rest are saved to "
                     f"unfetched_{domain_key}.json.")
            self.dump_unfetched(domain_key,
                                [(0, u, s) for u, s in leftover])
        for depth, url, source in queue:
            if stop_requested.is_set():
                self.log(f"{domain_key}: stop requested during second pass — "
                         f"remaining URLs dumped to unfetched file.")
                self.dump_unfetched(domain_key, [(0, u, s)
                                                 for u, s in url_source_pairs])
                return
            norm = normalize_url(url)
            with self.lock:
                if norm in self.visited:
                    continue
                self.visited.add(norm)
            skip_reason = should_skip(url)
            if skip_reason:
                self.write_record({
                    "url": url, "normalized_url": norm, "domain": domain_key,
                    "depth": depth, "discovered_from": source,
                    "outcome": f"skipped — {skip_reason}",
                    "fetched_at": now_iso()})
                continue
            if PDF_EXTENSION.search(url.lower()):
                self.write_record({
                    "url": url, "normalized_url": norm, "domain": domain_key,
                    "depth": depth, "discovered_from": source, "is_pdf": True,
                    "outcome": "recorded link only — PDF document, body not "
                               "downloaded",
                    "fetched_at": now_iso()})
                continue
            if not rp.can_fetch(USER_AGENT, url) and not rp.can_fetch("*", url):
                self.write_record({
                    "url": url, "normalized_url": norm, "domain": domain_key,
                    "depth": depth, "discovered_from": source,
                    "outcome": "skipped — robots.txt disallows it",
                    "fetched_at": now_iso()})
                continue
            time.sleep(delay)
            record = {"url": url, "normalized_url": norm, "domain": domain_key,
                      "depth": depth, "discovered_from": source,
                      "fetched_at": now_iso()}
            try:
                final_url, status, ctype, body = self.fetch_with_retry(
                    domain_key, url, delay)
                record["final_url"] = final_url
                record["http_status"] = status
                record["content_type"] = (ctype or "").split(";")[0]
                record["bytes"] = len(body)
                if "html" in (ctype or "").lower() and url_domain_key(final_url):
                    html = decode_html(body, ctype)
                    parser = LinkAndTitleParser()
                    try:
                        parser.feed(html)
                    except Exception:
                        pass
                    record["title"] = parser.title
                    digest = hashlib.sha1(
                        normalize_url(final_url).encode()).hexdigest()[:16]
                    rel_path = os.path.join("data", "raw", domain_key,
                                            f"{digest}.html")
                    abs_path = os.path.join(PROJECT_ROOT, rel_path)
                    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                    with open(abs_path, "w", encoding="utf-8") as fh:
                        fh.write(html)
                    record["html_path"] = rel_path
                    record["outcome"] = "fetched ok (second pass)"
                else:
                    record["outcome"] = (
                        f"fetched (second pass) but content-type "
                        f"'{record['content_type']}' is not HTML — body not "
                        f"saved")
                self.stats[domain_key]["fetched"] += 1
            except Exception as exc:
                self.stats[domain_key]["failed"] += 1
                record["outcome"] = (f"failed — {exc.__class__.__name__}: "
                                     f"{exc}")
            self.write_record(record)

    def crawl_domain_safe(self, domain_key):
        try:
            self.crawl_domain(domain_key)
        except Exception as exc:
            self.log(f"{domain_key}: worker crashed with "
                     f"{exc.__class__.__name__}: {exc} — other domains "
                     f"continue. Check the traceback in the log.")
            import traceback
            with self.lock:
                self.log_fh.write(traceback.format_exc() + "\n")
                self.log_fh.flush()

    def finish(self):
        minutes = (time.time() - self.started_at) / 60
        with open(EXTERNAL_DOMAINS_PATH, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "explanation": (
                        "Domains linked from crawled ABC pages that were NOT "
                        "crawled because they are outside the configured ABC "
                        "scope. Review this list to spot any ABC-related "
                        "property that should be added to DOMAINS in "
                        "scrape/crawl.py."),
                    "link_counts": dict(sorted(
                        self.external_domains.items(),
                        key=lambda kv: -kv[1])),
                },
                fh, indent=2)
        summary = {
            "finished_at": now_iso(),
            "duration_minutes": round(minutes, 1),
            "per_domain": self.stats,
            "total_records": self.records_written,
        }
        with open(SUMMARY_PATH, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        self.log("=" * 70)
        self.log(f"Crawl finished in {minutes:.1f} minutes. Per-domain "
                 f"results:")
        for key, st in self.stats.items():
            if not self.domain_filter or key in self.domain_filter:
                capped = (f", {st['capped']} left unfetched at cap"
                          if st["capped"] else "")
                self.log(f"  {key}: {st['fetched']} fetched, "
                         f"{st['skipped']} skipped, {st['failed']} failed"
                         f"{capped}")
        self.log(f"Index: {INDEX_PATH}")
        self.log(f"External (uncrawled) domains report: "
                 f"{EXTERNAL_DOMAINS_PATH}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--domains",
                    help="comma-separated domain keys to crawl (default all)")
    ap.add_argument("--max-pages", type=int,
                    help="override per-domain page cap (for smoke tests)")
    args = ap.parse_args()
    domain_filter = None
    if args.domains:
        domain_filter = set(args.domains.split(","))
        unknown = domain_filter - set(DOMAINS)
        if unknown:
            sys.exit(f"Unknown domain keys: {', '.join(unknown)}. "
                     f"Valid keys: {', '.join(DOMAINS)}")

    crawler = Crawler(domain_filter, args.max_pages)

    def on_signal(signum, frame):
        crawler.log("Stop signal received — workers will dump their queues "
                    "and exit cleanly. Re-running the script resumes where "
                    "this run left off.")
        stop_requested.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    crawler.run()


if __name__ == "__main__":
    main()
