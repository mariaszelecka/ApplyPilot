"""jobs.ch discovery -- scrapes Switzerland's largest job board.

jobs.ch renders job listings as server-side HTML (no JS needed), with a
predictable URL pattern and consistent listing structure. This makes it
scrapeable with plain HTTP requests + an HTML parser, no headless browser
required.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

BASE_URL = "https://www.jobs.ch/en/vacancies/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
REQUEST_DELAY_SECONDS = 2.0
MAX_PAGES_PER_SEARCH = 5


def _fetch_page(term: str, location: str, page: int = 1) -> str | None:
    params = {"term": term}
    if location:
        params["location"] = location
    if page > 1:
        params["page"] = str(page)

    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log.error("jobs.ch: fetch failed for %r page %d: %s", term, page, e)
        return None


def _parse_posted_hours(text: str) -> float | None:
    """Parse jobs.ch's leading relative-date text into an approximate age in hours.

    jobs.ch listings start with a phrase like "3 days ago", "37 minutes ago",
    "Last week", "Last month", or "New". Returns None if unrecognized -- callers
    should treat that as "unknown age" and keep the listing rather than drop it.
    """
    text = text.strip()

    if re.match(r"^(new|today)\b", text, re.IGNORECASE):
        return 0.0
    if re.match(r"^yesterday\b", text, re.IGNORECASE):
        return 24.0
    if re.match(r"^last week\b", text, re.IGNORECASE):
        return 7 * 24.0
    if re.match(r"^last month\b", text, re.IGNORECASE):
        return 30 * 24.0

    m = re.match(r"^(\d+)\s+(minute|hour|day|week|month)s?\s+ago\b", text, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        hours_per_unit = {"minute": 1 / 60, "hour": 1, "day": 24, "week": 24 * 7, "month": 24 * 30}
        return n * hours_per_unit[unit]

    return None


def _parse_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []

    for a in soup.select('a[href*="/vacancies/detail/"]'):
        href = a.get("href", "")
        if not href or "/vacancies/detail/" not in href:
            continue

        url = href if href.startswith("http") else f"https://www.jobs.ch{href}"
        title = a.get("title") or a.get_text(strip=True)
        if not title:
            continue

        text_block = a.get_text(" ", strip=True)

        location_match = re.search(r"Place of work:\s*([^\u00b7]+?)(?:Workload|Contract|$)", text_block)
        location = location_match.group(1).strip() if location_match else None

        company = None
        company_match = re.search(r"position\s*([A-Z][^\u00b7]+?)(?:Easy apply|New|$)", text_block)
        if company_match:
            company = company_match.group(1).strip()

        jobs.append({
            "title": title,
            "url": url,
            "location": location,
            "company": company,
            "posted_hours_ago": _parse_posted_hours(text_block),
        })

    seen = set()
    unique = []
    for j in jobs:
        if j["url"] in seen:
            continue
        seen.add(j["url"])
        unique.append(j)

    return unique


def _run_one_search(term: str, location: str, max_results: int = 30,
                    hours_old: float | None = None) -> list[dict]:
    all_jobs: list[dict] = []

    for page in range(1, MAX_PAGES_PER_SEARCH + 1):
        html = _fetch_page(term, location, page)
        if html is None:
            break

        page_jobs = _parse_listings(html)
        if not page_jobs:
            break

        all_jobs.extend(page_jobs)
        log.info("jobs.ch [%s in %s] page %d: %d listings (total so far %d)",
                 term, location or "anywhere", page, len(page_jobs), len(all_jobs))

        if len(all_jobs) >= max_results:
            all_jobs = all_jobs[:max_results]
            break

        time.sleep(REQUEST_DELAY_SECONDS)

    if hours_old is not None:
        before = len(all_jobs)
        # Keep jobs with an unparseable/unknown age too -- better to over-include
        # than to silently drop a real listing because of a parsing gap.
        all_jobs = [j for j in all_jobs if j["posted_hours_ago"] is None or j["posted_hours_ago"] <= hours_old]
        dropped = before - len(all_jobs)
        if dropped:
            log.info("jobs.ch [%s in %s]: dropped %d listing(s) older than %gh",
                      term, location or "anywhere", dropped, hours_old)

    return all_jobs


def store_results(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    job.get("title"),
                    None,
                    None,
                    job.get("location"),
                    job.get("company") or "jobs.ch",
                    "jobsch",
                    now,
                    None,
                    url,
                    None,
                ),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def run_jobsch_discovery(searches: list[dict]) -> dict:
    init_db()
    conn = get_connection()

    total_new = 0
    total_existing = 0

    for s in searches:
        term = s.get("search_term", "")
        location = s.get("location", "").split(",")[0].strip()
        max_results = s.get("results_wanted", 30)
        hours_old = s.get("hours_old")

        if not term:
            continue

        jobs = _run_one_search(term, location, max_results=max_results, hours_old=hours_old)
        new, existing = store_results(conn, jobs)
        total_new += new
        total_existing += existing

        log.info("jobs.ch [%s in %s]: %d new, %d dupes", term, location, new, existing)

    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    log.info("jobs.ch discovery complete: %d new | %d dupes | %d total in DB",
              total_new, total_existing, db_total)

    return {"new": total_new, "existing": total_existing, "db_total": db_total}