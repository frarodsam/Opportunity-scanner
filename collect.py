#!/usr/bin/env python3
"""
Insight Pixels opportunity collector.

Pulls new postings from ReliefWeb, World Bank procurement notices, the UN
Global Marketplace, and a list of watched web pages (INGO bid pages and similar), removes anything already
seen, and writes:

  data/weekly/YYYY-MM-DD.json   new postings with full text, for AI screening
                                (for sites that only give a title and a link,
                                the script opens each new link and saves the text)
  data/weekly/YYYY-MM-DD.csv    the same postings as a table, for people
  data/seen.json                IDs already collected, so nothing repeats

No AI is used here. This script only collects. Screening against the Insight
Pixels rubric happens afterwards.

Uses only the Python standard library, so there is nothing to install.

Usage:
  python collect.py                 normal weekly run
  python collect.py --days 30       look further back (first run, catch-up)
  python collect.py --debug         print one raw record per source and exit
"""

import argparse
import csv
import json
import os
import re
import html as htmllib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
WEEKLY = DATA / "weekly"
SEEN_FILE = DATA / "seen.json"
CONFIG_FILE = ROOT / "config.json"

RELIEFWEB_URL = "https://api.reliefweb.int/v2/jobs"
WORLDBANK_URL = "https://search.worldbank.org/api/v2/procnotices"

CSV_COLUMNS = [
    "source", "title", "organization", "country", "posting_type",
    "posted", "deadline", "days_left", "flags", "keyword_hits", "url",
]


# ---------------------------------------------------------------- helpers

def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def http_json(url, body=None, timeout=60):
    """GET (or POST if body is given) and return parsed JSON."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "insight-pixels-opportunity-scanner",
        },
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d",
    "%d-%b-%Y", "%d %b %Y", "%m/%d/%Y", "%Y/%m/%d",
)


def parse_date(value):
    """Return a date from the formats these APIs use, or None."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        for candidate in (text, text[:19], text[:10], text[:11]):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def names(items):
    """ReliefWeb returns lists of {name: ...}; join them into one string."""
    if not items:
        return ""
    if isinstance(items, dict):
        items = [items]
    return "; ".join(str(i.get("name", "")) for i in items if isinstance(i, dict))


def keyword_hits(text, keywords):
    low = text.lower()
    return [k for k in keywords if k.lower() in low]


def strip_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def finish(posting, cfg, today):
    """Add the computed columns every posting shares."""
    deadline = parse_date(posting.get("deadline"))
    posting["deadline"] = deadline.isoformat() if deadline else ""
    posting["days_left"] = (deadline - today).days if deadline else ""
    flags = list(posting.get("flags", []))
    if deadline and (deadline - today).days < cfg["short_runway_days"]:
        flags.append("short runway")
    if not deadline and posting.get("text"):
        flags.append("no deadline found")
    posting["flags"] = "; ".join(flags)
    searchable = posting["title"] + " " + posting.get("text", "")
    posting["keyword_hits"] = "; ".join(keyword_hits(searchable, cfg["keywords"]))
    return posting


# ---------------------------------------------------------------- ReliefWeb

def reliefweb_request(appname, since, extra_filter=None, query=None):
    """Page through ReliefWeb jobs created since a date."""
    conditions = [{"field": "date.created", "value": {"from": since.isoformat() + "T00:00:00+00:00"}}]
    if extra_filter:
        conditions.append(extra_filter)
    body = {
        "limit": 500,
        "offset": 0,
        "sort": ["date.created:desc"],
        "filter": {"operator": "AND", "conditions": conditions},
        "fields": {"include": [
            "title", "body", "how_to_apply", "url", "date.created", "date.closing",
            "source.name", "country.name", "type.name",
            "career_categories.name", "theme.name",
        ]},
    }
    if query:
        body["query"] = query
    url = RELIEFWEB_URL + "?" + urllib.parse.urlencode({"appname": appname})
    out = []
    while True:
        page = http_json(url, body).get("data", [])
        out.extend(page)
        if len(page) < body["limit"] or len(out) >= 5000:
            return out
        body["offset"] += body["limit"]


def collect_reliefweb(cfg, since, today, debug=False):
    appname = os.environ.get("RELIEFWEB_APPNAME", "").strip()
    if not appname:
        print("ReliefWeb: skipped. RELIEFWEB_APPNAME is not set (see README).")
        return []

    # 1) Everything posted as a consultancy.
    raw = reliefweb_request(
        appname, since,
        extra_filter={"field": "type.name", "value": cfg["reliefweb"]["types"], "operator": "OR"},
    )
    # 2) Anything else whose title looks like a tender (RFPs are sometimes
    #    posted under the "Job" type).
    title_terms = " OR ".join('"%s"' % t for t in cfg["reliefweb"]["title_terms"])
    raw += reliefweb_request(
        appname, since,
        query={"value": title_terms, "fields": ["title"], "operator": "OR"},
    )

    if debug:
        print(json.dumps(raw[:1], indent=2)[:3000])
        return []

    postings = []
    for item in raw:
        f = item.get("fields", {})
        dates = f.get("date", {}) or {}
        ptype = names(f.get("type"))
        flags = []
        if ptype and ptype not in cfg["reliefweb"]["types"]:
            flags.append("not listed as consultancy: check it is not a staff vacancy")
        text = (f.get("body") or "") + "\n\nHOW TO APPLY\n" + (f.get("how_to_apply") or "")
        postings.append(finish({
            "id": "reliefweb:%s" % item.get("id"),
            "source": "ReliefWeb",
            "title": f.get("title", "").strip(),
            "organization": names(f.get("source")),
            "country": names(f.get("country")),
            "posting_type": ptype,
            "categories": names(f.get("career_categories")),
            "themes": names(f.get("theme")),
            "posted": (parse_date(dates.get("created")) or today).isoformat(),
            "deadline": dates.get("closing"),
            "url": f.get("url", ""),
            "text": text[: cfg["max_text_chars"]],
            "flags": flags,
        }, cfg, today))
    print("ReliefWeb: %d postings since %s" % (len(postings), since))
    return postings


# ---------------------------------------------------------------- World Bank

def first(record, *keys):
    for k in keys:
        if record.get(k):
            return record[k]
    return ""


def collect_worldbank(cfg, since, today, debug=False):
    wanted = [t.lower() for t in cfg["worldbank"]["notice_types"]]
    postings, offset, rows = [], 0, 100

    for _ in range(cfg["worldbank"]["max_pages"]):
        params = {"format": "json", "rows": rows, "os": offset,
                  "srt": "noticedate", "order": "desc"}
        payload = http_json(WORLDBANK_URL + "?" + urllib.parse.urlencode(params))
        notices = payload.get("procnotices") or payload.get("notices") or []
        if isinstance(notices, dict):
            notices = list(notices.values())
        if debug:
            print(json.dumps(notices[:1], indent=2)[:3000])
            return []
        if not notices:
            break

        any_recent = False
        for n in notices:
            posted = parse_date(first(n, "noticedate", "notice_date", "submission_date"))
            if posted and posted < since:
                continue
            any_recent = True
            ntype = str(first(n, "notice_type"))
            if ntype.lower() not in wanted:
                continue
            method = str(first(n, "procurement_method_name", "procurement_method_code"))
            ref = str(first(n, "bid_reference_no"))
            flags = []
            if "individual" in method.lower() or "INDV" in ref.upper():
                flags.append("individual consultant selection: roster question")
            nid = str(first(n, "id"))
            postings.append(finish({
                "id": "worldbank:%s" % nid,
                "source": "World Bank",
                "title": strip_html(str(first(n, "bid_description", "project_name"))),
                "organization": strip_html(str(first(n, "contact_organization", "contact_agency"))),
                "country": str(first(n, "project_ctry_name", "contact_ctry_name")),
                "posting_type": ntype + (" / " + method if method else ""),
                "categories": str(first(n, "procurement_group")),
                "themes": "",
                "posted": posted.isoformat() if posted else "",
                "deadline": first(n, "submission_deadline_date", "submission_date", "deadline_date"),
                "url": "https://projects.worldbank.org/en/projects-operations/procurement-detail/" + nid,
                "text": strip_html(str(first(n, "notice_text")))[: cfg["max_text_chars"]],
                "project": "%s %s" % (first(n, "project_id"), first(n, "project_name")),
                "reference": ref,
                "flags": flags,
            }, cfg, today))
        # Results are newest first, so a whole page of old notices means we are done.
        if not any_recent:
            break
        offset += rows

    print("World Bank: %d postings since %s" % (len(postings), since))
    return postings


# ---------------------------------------------------------------- UN Global Marketplace

UNGM_URL = "https://www.ungm.org/Public/Notice/Search"
UNGM_DATE = re.compile(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b")


def http_post_text(url, body, timeout=60):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/html, */*",
                 "X-Requested-With": "XMLHttpRequest",
                 "User-Agent": "Mozilla/5.0 (compatible; insight-pixels-opportunity-scanner)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def collect_ungm(cfg, since, today, debug=False):
    """
    UN Global Marketplace: where UNICEF, UNESCO and most other UN agencies
    publish tenders. It has no official public API, so this uses the same
    search request the website itself makes, once per title keyword.
    If UNGM changes its site this source will fail on its own without
    affecting the others.
    """
    settings = cfg.get("ungm") or {}
    if not settings.get("title_keywords"):
        return []
    agencies = [a.lower() for a in settings.get("agencies", [])]
    found = {}
    for word in settings["title_keywords"]:
        body = {
            "PageIndex": 0, "PageSize": 100, "Title": word, "Description": "",
            "Reference": "", "PublishedFrom": since.strftime("%d-%b-%Y"), "PublishedTo": "",
            "DeadlineFrom": today.strftime("%d-%b-%Y"), "DeadlineTo": "",
            "Countries": [], "Agencies": [], "UNSPSCs": [], "NoticeTypes": [],
            "SortField": "DatePublished", "SortAscending": False, "isPicker": False,
            "IsSustainable": False, "IsActive": True, "NoticeDisplayType": None,
            "NoticeSearchTotalLabelId": "noticeSearchTotal", "TypeOfCompetitions": [],
        }
        html = http_post_text(UNGM_URL, body)
        if debug:
            print(html[:3000])
            return []
        # Each result row carries its notice number; the text between two
        # markers is one row.
        parts = re.split(r'data-noticeid="(\d+)"', html)
        for i in range(1, len(parts) - 1, 2):
            nid = parts[i]
            cells = [c.strip() for c in re.split(r"<[^>]+>", parts[i + 1]) if c.strip()]
            cells = [c for c in cells if c.lower() not in ("open in a new window",)]
            if not cells or nid in found:
                continue
            row_text = " | ".join(cells)
            if agencies and not any(a in row_text.lower() for a in agencies):
                continue
            dates = sorted(d for d in (parse_date(m) for m in UNGM_DATE.findall(row_text)) if d)
            found[nid] = finish({
                "id": "ungm:%s" % nid,
                "source": "UNGM",
                "title": max(cells[:4], key=len),
                "organization": next((c for c in cells if c.lower() in agencies or c.isupper() and 2 < len(c) < 12), ""),
                "country": "",
                "posting_type": "UN tender notice",
                "categories": "", "themes": "",
                "posted": dates[0].isoformat() if dates else "",
                "deadline": dates[-1].isoformat() if len(dates) > 1 else None,
                "url": "https://www.ungm.org/Public/Notice/%s" % nid,
                "text": row_text[: cfg["max_text_chars"]],
                "flags": ["summary only: open the link for the full notice"],
            }, cfg, today)
    print("UNGM: %d notices since %s" % (len(found), since))
    return list(found.values())


# ---------------------------------------------------------------- watched pages

class LinkParser(HTMLParser):
    """Collects (href, visible text) for every link on a page."""

    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def http_text(url, timeout=60):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; insight-pixels-opportunity-scanner)",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def collect_pages(cfg, since, today, debug=False):
    """
    Generic watcher for sites with no API. It does not understand the page.
    It lists the links on it, and any link not seen on a previous run is
    reported as a possible new posting. Works for plain pages that list
    opportunities as links. Does not work for pages that load their content
    with JavaScript or sit behind a login.
    """
    postings, failed = [], []
    for page in cfg.get("watched_pages", []):
        name, url = page["name"], page["url"]
        try:
            html = http_text(url)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as exc:
            failed.append(name)
            print("  %s: FAILED (%s)" % (name, exc))
            continue
        parser = LinkParser()
        parser.feed(html)
        pattern = re.compile(page["link_pattern"], re.I) if page.get("link_pattern") else None
        found = {}
        for href, text in parser.links:
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            link = urllib.parse.urljoin(url, href).split("#")[0]
            if pattern:
                if not pattern.search(link) and not pattern.search(text):
                    continue
            elif len(text) < cfg["min_link_text_chars"]:
                continue  # menus and buttons have short labels; postings have titles
            found.setdefault(link, text or link)
        if debug:
            print(name, json.dumps(list(found.items())[:15], indent=2))
            continue
        for link, text in found.items():
            postings.append(finish({
                "id": "page:%s" % link,
                "source": name,
                "title": text,
                "organization": name,
                "country": "",
                "posting_type": "link on watched page",
                "categories": "", "themes": "",
                "posted": today.isoformat(),
                "deadline": None,
                "url": link,
                "text": "",
                "flags": ["open the link for details"],
            }, cfg, today))
        note = "" if found else "  (none found: the page may load its listings with JavaScript)"
        print("  %s: %d links%s" % (name, len(found), note))
    if failed and len(failed) == len(cfg.get("watched_pages", [])):
        raise ValueError("every watched page failed")
    if failed:
        print("Watched pages that failed: " + ", ".join(failed))
    print("Watched pages: %d links in total" % len(postings))
    return postings


# ---------------------------------------------------------------- posting details

def fetch_detail(url, timeout=30):
    """Open one posting. Returns (kind, text): kind is 'html', 'pdf' or 'other'."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; insight-pixels-opportunity-scanner)",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
            return "pdf", ""
        if "html" not in ctype and "text" not in ctype:
            return "other", ""
        return "html", resp.read(2_000_000).decode("utf-8", errors="replace")


def page_text(html):
    """Readable text of a web page, without menus, scripts and styling."""
    for tag in ("script", "style", "noscript", "nav", "header", "footer", "form", "svg"):
        html = re.sub(r"(?is)<%s\b.*?</%s>" % (tag, tag), " ", html)
    return htmllib.unescape(strip_html(html))


def add_details(postings, cfg):
    """
    For postings that arrived as a title and a link only, open the link and
    save the page text, so screening is based on the posting itself.
    PDFs and pages that refuse the request are flagged instead.
    """
    if not cfg.get("fetch_details", True):
        return
    todo = [p for p in postings if p["id"].startswith(("page:", "ungm:"))]
    todo = todo[: cfg.get("max_detail_fetches", 250)]
    got = pdfs = failed = 0
    for p in todo:
        flags = [f for f in p["flags"].split("; ")
                 if f and not f.startswith(("open the link", "summary only"))]
        try:
            kind, raw = fetch_detail(p["url"])
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError, OSError):
            kind, raw = "failed", ""
        text = page_text(raw) if kind == "html" else ""
        if len(text) >= 200:
            summary = p["text"]
            p["text"] = ((summary + "\n\n") if summary else "") + text
            p["text"] = p["text"][: cfg["max_text_chars"]]
            p["keyword_hits"] = "; ".join(keyword_hits(p["title"] + " " + p["text"], cfg["keywords"]))
            got += 1
        elif kind == "pdf":
            flags.append("posting is a PDF: title only, open the link")
            pdfs += 1
        else:
            flags.append("could not read the posting: title only, open the link")
            failed += 1
        p["flags"] = "; ".join(flags)
        time.sleep(cfg.get("detail_pause_seconds", 0.5))  # be polite to the sites
    print("Posting details: %d read, %d PDFs, %d could not be read" % (got, pdfs, failed))


# ---------------------------------------------------------------- main

def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, help="how many days back to look")
    parser.add_argument("--debug", action="store_true", help="print one raw record per source")
    args = parser.parse_args(argv)

    cfg = load_config()
    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=args.days or cfg["lookback_days"])
    WEEKLY.mkdir(parents=True, exist_ok=True)

    collected, failures = [], []
    sources = (("ReliefWeb", collect_reliefweb), ("World Bank", collect_worldbank),
               ("UNGM", collect_ungm), ("Watched pages", collect_pages))
    for name, fn in sources:
        try:
            collected += fn(cfg, since, today, debug=args.debug)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as exc:
            # One source failing should not stop the others.
            failures.append(name)
            print("%s: FAILED (%s)" % (name, exc))
    if args.debug:
        return 0

    seen = load_seen()
    new, batch_ids = [], set()
    for p in collected:
        if p["id"] in seen or p["id"] in batch_ids:
            continue
        if p["days_left"] != "" and p["days_left"] < 0:
            continue  # already closed
        batch_ids.add(p["id"])
        new.append(p)
    add_details(new, cfg)
    # Most relevant-looking first: more keyword hits, then more time left.
    new.sort(key=lambda p: (-len([k for k in p["keyword_hits"].split("; ") if k]),
                            -(p["days_left"] if p["days_left"] != "" else 0)))

    stamp = today.isoformat()
    # A second run on the same day adds to that day's file instead of replacing it.
    day_file = WEEKLY / (stamp + ".json")
    if day_file.exists():
        try:
            earlier = json.loads(day_file.read_text(encoding="utf-8")).get("postings", [])
        except ValueError:
            earlier = []
        have = {p["id"] for p in new}
        new_count = len(new)
        new = new + [p for p in earlier if p["id"] not in have]
        print("Kept %d postings from an earlier run today." % (len(new) - new_count))
    (WEEKLY / (stamp + ".json")).write_text(
        json.dumps({"collected_on": stamp, "since": since.isoformat(),
                    "failed_sources": failures, "count": len(new), "postings": new},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    with open(WEEKLY / (stamp + ".csv"), "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(new)

    for p in new:
        seen[p["id"]] = stamp
    SEEN_FILE.write_text(json.dumps(seen, indent=0, sort_keys=True), encoding="utf-8")

    print("New postings this run: %d (written to data/weekly/%s.json and .csv)" % (len(new), stamp))
    # Fail the run only if every source failed, so GitHub flags it.
    return 1 if len(failures) == len(sources) else 0


if __name__ == "__main__":
    sys.exit(main())
