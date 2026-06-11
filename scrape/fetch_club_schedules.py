#!/usr/bin/env python3
"""
Fetch every club's "Upcoming Educational Courses" schedule from
americasboatingclub.org/club-details/?sqdno=<squadron number>.

How this works, in plain English:
  data/interactive/clubs.json (produced by scrape/fetch_club_locations.py)
  gives us each club's squadron number. The club-details page for a squadron
  renders that club's upcoming classes server-side: class title, link to the
  course description, start date, a registration link whose code starts with
  C- (course) or S- (seminar), and the venue address. This script fetches
  each club's page politely (1.2 s apart), parses those fields, and writes
  one combined national class schedule to data/interactive/class_schedule.json.

  Each page's raw HTML is kept in data/interactive/club_details_html/ so any
  parsed value can be traced back to exactly what the site served.

Run it directly:  python3 scrape/fetch_club_schedules.py
Stdlib only — no third-party dependencies.
"""

import base64
import html as html_mod
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLUBS_PATH = os.path.join(PROJECT_ROOT, "data", "interactive", "clubs.json")
HTML_DIR = os.path.join(PROJECT_ROOT, "data", "interactive",
                        "club_details_html")
OUT_PATH = os.path.join(PROJECT_ROOT, "data", "interactive",
                        "class_schedule.json")

BASE_URL = "https://americasboatingclub.org/club-details/?sqdno="
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DELAY_SECONDS = 1.2

CLASS_BLOCK = re.compile(
    r'<a href="(?P<course_url>[^"]+)"><b>(?P<title>[^<]+)</b></a><br>'
    r'\s*Class begins\s+(?P<begins>[A-Z][a-z]{2} \d{1,2} \d{4})'
    r'(?:<br>\s*<a href="(?P<reg_url>[^"]+)"[^>]*>Click here to register</a>)?',
)
# Some clubs' pages render blank rows: an empty title linking to "/" with the
# Unix-epoch date "Class begins Jan 01 1970" and no registration code. They
# are placeholder database rows, not real classes; we count them so the log
# can say how many were excluded.
PLACEHOLDER_BLOCK = re.compile(
    r'<a href="/"><b></b></a><br>\s*Class begins\s+Jan 0?1 1970')
VENUE_BLOCK = re.compile(
    r'<div style="float:left; width: 55%;">(?P<venue>.*?)</div>', re.DOTALL)
HIDDEN_MAIL = re.compile(
    r'<joomla-hidden-mail[^>]*first="([^"]*)"[^>]*last="([^"]*)"')
REG_CODE = re.compile(r'\?([CS])-(\d+)')


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def decode_hidden_mail(first_b64, last_b64):
    """Joomla hides emails as base64 user/domain halves; put them back."""
    try:
        user = base64.b64decode(first_b64).decode("utf-8", errors="replace")
        domain = base64.b64decode(last_b64).decode("utf-8", errors="replace")
        return f"{user}@{domain}"
    except Exception:
        return None


def clean_venue(venue_html):
    """Turn the venue HTML block into a list of plain-text lines."""
    venue_html = HIDDEN_MAIL.sub("", venue_html)
    venue_html = re.sub(r"<joomla-hidden-mail.*?</joomla-hidden-mail>", "",
                        venue_html, flags=re.DOTALL)
    venue_html = re.sub(r"This email address is being protected.*?view it\.",
                        "", venue_html, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", venue_html)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [html_mod.unescape(l.strip()) for l in text.splitlines()]
    return [l for l in lines if l]


def parse_schedule(page_html, club):
    """Extract the list of upcoming classes from one club-details page.
    Returns (classes, empty_reason, placeholders_excluded)."""
    section_start = page_html.find("Upcoming Educational Courses")
    if section_start == -1:
        return [], "page has no 'Upcoming Educational Courses' section", 0
    section = page_html[section_start:]
    placeholders = len(PLACEHOLDER_BLOCK.findall(section))
    # The section runs to the end of its enclosing layout block; the classes
    # are <p>-wrapped pairs of (class info, venue info).
    classes = []
    for para in re.split(r"</p>", section):
        m = CLASS_BLOCK.search(para)
        if not m:
            continue
        venue_match = VENUE_BLOCK.search(para)
        venue_lines = clean_venue(venue_match.group("venue")) if venue_match else []
        email_match = HIDDEN_MAIL.search(para)
        contact_email = (decode_hidden_mail(*email_match.groups())
                         if email_match else None)
        reg_url = m.group("reg_url")
        code_match = REG_CODE.search(reg_url or "")
        kind = None
        if code_match:
            kind = "course" if code_match.group(1) == "C" else "seminar"
        course_url = m.group("course_url")
        if course_url.startswith("/"):
            course_url = "https://americasboatingclub.org" + course_url
        classes.append({
            "club_name": club["name"],
            "sqdno": club["sqdno"],
            "club_state": club.get("state"),
            "title": html_mod.unescape(m.group("title")).strip(),
            "course_url": course_url,
            "begins": m.group("begins"),
            "registration_url": reg_url,
            "registration_code": (f"{code_match.group(1)}-{code_match.group(2)}"
                                  if code_match else None),
            "kind": kind,
            "venue_lines": venue_lines,
            "contact_email": contact_email,
        })
    if not classes:
        return [], ("schedule section exists but lists no classes — this "
                    "club has nothing scheduled right now"), placeholders
    return classes, None, placeholders


def main():
    os.makedirs(HTML_DIR, exist_ok=True)
    with open(CLUBS_PATH, encoding="utf-8") as fh:
        clubs = json.load(fh)["clubs"]
    log(f"Fetching upcoming-class schedules for {len(clubs)} clubs from "
        f"{BASE_URL}<sqdno> (one request every {DELAY_SECONDS}s — about "
        f"{len(clubs) * DELAY_SECONDS / 60:.0f} minutes)...")

    all_classes = []
    per_club = []
    failures = 0
    placeholders_total = 0
    for i, club in enumerate(clubs, 1):
        sqdno = club["sqdno"]
        url = BASE_URL + sqdno
        cache_path = os.path.join(HTML_DIR, f"sqdno_{sqdno}.html")
        page_html = None
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as fh:
                page_html = fh.read()
            note = "from cache (already fetched in a previous run)"
        else:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=45) as resp:
                    page_html = resp.read().decode("utf-8", errors="replace")
                with open(cache_path, "w", encoding="utf-8") as fh:
                    fh.write(page_html)
                note = "fetched"
                time.sleep(DELAY_SECONDS)
            except Exception as exc:
                failures += 1
                log(f"  {club['name']} (sqdno {sqdno}): FAILED — "
                    f"{exc.__class__.__name__}: {exc}. Re-run the script to "
                    f"retry just the failures (successes are cached).")
                per_club.append({"sqdno": sqdno, "club": club["name"],
                                 "classes": 0,
                                 "outcome": f"fetch failed: {exc}"})
                time.sleep(DELAY_SECONDS)
                continue
        classes, empty_reason, placeholders = parse_schedule(page_html, club)
        all_classes.extend(classes)
        placeholders_total += placeholders
        outcome = empty_reason or f"{len(classes)} classes parsed"
        if placeholders:
            outcome += (f" ({placeholders} blank epoch-dated placeholder "
                        f"rows excluded)")
        per_club.append({
            "sqdno": sqdno, "club": club["name"],
            "classes": len(classes),
            "outcome": outcome,
        })
        if i % 25 == 0:
            log(f"  progress: {i}/{len(clubs)} clubs, "
                f"{len(all_classes)} classes so far ({note}).")

    with_classes = sum(1 for p in per_club if p["classes"])
    log(f"Done: {len(all_classes)} upcoming classes across {with_classes} of "
        f"{len(clubs)} clubs ({failures} clubs failed to fetch; "
        f"{placeholders_total} blank placeholder rows — empty title, date "
        f"'Jan 01 1970' — were excluded as not being real classes).")

    output = {
        "_provenance": {
            "description": (
                "National upcoming-class schedule assembled by fetching each "
                "club's club-details page (the same page a visitor sees after "
                "using Find a Club) and parsing its server-rendered 'Upcoming "
                "Educational Courses' section."),
            "source_url_pattern": BASE_URL + "<sqdno>",
            "script": "scrape/fetch_club_schedules.py",
            "fetched_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "clubs_checked": len(clubs),
            "clubs_with_classes": with_classes,
            "fetch_failures": failures,
            "placeholder_rows_excluded": placeholders_total,
            "raw_html_dir": "data/interactive/club_details_html/",
        },
        "per_club": per_club,
        "classes": all_classes,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=1, ensure_ascii=False)
    log(f"Schedule written to {OUT_PATH}")


if __name__ == "__main__":
    main()
