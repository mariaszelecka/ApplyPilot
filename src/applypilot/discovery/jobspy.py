"""JobSpy-based job discovery: searches Indeed, LinkedIn.

Uses python-jobspy to scrape multiple job boards, deduplicates results,
parses salary ranges, and stores everything in the ApplyPilot database.

Search queries, locations, and filtering rules are loaded from the user's
search configuration YAML (searches.yaml) rather than being hardcoded.
"""

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone

from jobspy import scrape_jobs

from applypilot import config
from applypilot.database import get_connection, init_db, store_jobs
from applypilot.discovery.freshness import looks_expired
from applypilot.scoring.scorer import is_consulting_manager_title, is_senior_title

log = logging.getLogger(__name__)


# -- Proxy parsing -----------------------------------------------------------

def parse_proxy(proxy_str: str) -> dict:
    """Parse host:port:user:pass into components."""
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return {
            "host": host,
            "port": port,
            "user": user,
            "pass": passwd,
            "jobspy": f"{user}:{passwd}@{host}:{port}",
            "playwright": {
                "server": f"http://{host}:{port}",
                "username": user,
                "password": passwd,
            },
        }
    elif len(parts) == 2:
        host, port = parts
        return {
            "host": host,
            "port": port,
            "user": None,
            "pass": None,
            "jobspy": f"{host}:{port}",
            "playwright": {"server": f"http://{host}:{port}"},
        }
    else:
        raise ValueError(
            f"Proxy format not recognized: {proxy_str}. "
            f"Expected: host:port:user:pass or host:port"
        )


# -- Retry wrapper -----------------------------------------------------------

def _scrape_with_retry(kwargs: dict, max_retries: int = 2, backoff: float = 5.0):
    """Call scrape_jobs with retry on transient failures."""
    for attempt in range(max_retries + 1):
        try:
            return scrape_jobs(**kwargs)
        except Exception as e:
            err = str(e).lower()
            transient = any(k in err for k in ("timeout", "429", "proxy", "connection", "reset", "refused"))
            if transient and attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("Retry %d/%d in %.0fs: %s", attempt + 1, max_retries, wait, e)
                time.sleep(wait)
            else:
                raise


# -- Location filtering ------------------------------------------------------

def _load_location_config(search_cfg: dict) -> tuple[list[str], list[str]]:
    """Extract accept/reject location lists from search config.

    Falls back to sensible defaults if not defined in the YAML.
    """
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    return accept, reject


# Region keyword groups: matched against a search's own `location` field to
# derive (a) which Indeed country code to query and (b) which location
# strings should be accepted when filtering results. Keyed by a keyword that
# must appear in the search's location string (lowercased).
_REGION_MAP: list[tuple[str, str, list[str]]] = [
    # (keyword-to-match-in-search-location, indeed_country_code, accept_locations)
    ("switzerland", "switzerland",
     ["switzerland", "zurich", "zürich", "zug", "basel", "bern", "st. gallen", "lausanne", "winterthur", "geneva"]),
    ("zurich", "switzerland",
     ["switzerland", "zurich", "zürich", "zug", "basel", "bern", "st. gallen", "lausanne", "winterthur", "geneva"]),
    ("united kingdom", "uk", ["united kingdom", "london", "uk", "england"]),
    ("london", "uk", ["united kingdom", "london", "uk", "england"]),
    ("ireland", "ireland", ["ireland", "dublin"]),
    ("dublin", "ireland", ["ireland", "dublin"]),
    ("monaco", "france", ["monaco", "nice", "france", "côte d'azur", "cote d'azur"]),
    ("nice", "france", ["nice", "monaco", "france", "côte d'azur", "cote d'azur"]),
    ("france", "france", ["france", "nice", "monaco", "côte d'azur", "cote d'azur"]),
    ("san francisco", "usa", ["san francisco", "silicon valley", "bay area", "california", "usa", "united states"]),
    ("silicon valley", "usa", ["san francisco", "silicon valley", "bay area", "california", "usa", "united states"]),
    ("california", "usa", ["san francisco", "silicon valley", "bay area", "california", "usa", "united states"]),
    ("united states", "usa", ["usa", "united states"]),
]


def _infer_region(location: str) -> tuple[str, list[str]]:
    """Infer (indeed_country_code, accept_locations) from a search's location string.

    Falls back to the bare city/country name as both the country code guess
    and the sole accept pattern when no known region matches -- better to
    under-filter an unrecognized location than to silently drop it.
    """
    loc_lower = (location or "").lower()
    for keyword, country_code, accept in _REGION_MAP:
        if keyword in loc_lower:
            return country_code, accept
    fallback = [p.strip() for p in loc_lower.split(",") if p.strip()]
    return (fallback[-1] if fallback else "usa"), (fallback or [loc_lower])




def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter.

    Remote jobs are always accepted. Non-remote jobs must match an accept
    pattern and not match a reject pattern.
    """
    if not location:
        return True  # unknown location -- keep it, let scorer decide

    loc = location.lower()

    # Remote jobs always OK
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True

    # Reject non-remote matches
    for r in reject:
        if r.lower() in loc:
            return False

    # Accept matches
    for a in accept:
        if a.lower() in loc:
            return True

    # No match -- reject unknown
    return False


# -- DB storage (JobSpy DataFrame -> SQLite) ---------------------------------

def _dedup_key(title: str | None, company: str | None) -> str | None:
    """Normalized (title, company) signature for cross-site duplicate
    detection. The same posting scraped from LinkedIn vs. Indeed gets
    completely different URLs (so the url UNIQUE constraint never catches
    it) and often differently-formatted locations ("Zurich, Zurich,
    Switzerland" vs. "Zürich, ZH, CH (Remote)") -- but title and company are
    reliably the same, so that's the dedup signal instead of location."""
    if not title or not company:
        return None
    t = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title.lower())).strip()
    c = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", company.lower())).strip()
    if not t or not c:
        return None
    return f"{t}|{c}"


def store_jobspy_results(conn: sqlite3.Connection, df, source_label: str) -> tuple[int, int]:
    """Store JobSpy DataFrame results into the DB. Returns (new, existing)."""
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    # Preload existing (title, company) signatures once so a job already in
    # the DB under a different site's URL doesn't get inserted again as a
    # near-identical duplicate under a new URL.
    seen_keys: set[str] = set()
    for t, c in conn.execute("SELECT title, company FROM jobs").fetchall():
        key = _dedup_key(t, c)
        if key:
            seen_keys.add(key)

    for _, row in df.iterrows():
        url = str(row.get("job_url", ""))
        if not url or url == "nan":
            continue

        title = str(row.get("title", "")) if str(row.get("title", "")) != "nan" else None
        company = str(row.get("company", "")) if str(row.get("company", "")) != "nan" else None
        location_str = str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None

        dedup_key = _dedup_key(title, company)
        if dedup_key and dedup_key in seen_keys:
            existing += 1
            continue

        # Build salary string from min/max
        salary = None
        min_amt = row.get("min_amount")
        max_amt = row.get("max_amount")
        interval = str(row.get("interval", "")) if str(row.get("interval", "")) != "nan" else ""
        currency = str(row.get("currency", "")) if str(row.get("currency", "")) != "nan" else ""
        if min_amt and str(min_amt) != "nan":
            if max_amt and str(max_amt) != "nan":
                salary = f"{currency}{int(float(min_amt)):,}-{currency}{int(float(max_amt)):,}"
            else:
                salary = f"{currency}{int(float(min_amt)):,}"
            if interval:
                salary += f"/{interval}"

        description = str(row.get("description", "")) if str(row.get("description", "")) != "nan" else None
        site_name = str(row.get("site", source_label))
        is_remote = row.get("is_remote", False)

        site_label = f"{site_name}"
        if is_remote:
            location_str = f"{location_str} (Remote)" if location_str else "Remote"

        strategy = "jobspy"

        # If JobSpy gave us a full description, promote it directly -- but
        # still run the cheap text-based expiry check against it. This is a
        # snapshot from scrape time, not a guarantee the posting is still
        # open; the tailoring stage does a live re-check on top of this
        # (see enrichment.detail.recheck_jobs) for the shortlist that
        # actually matters, since we can't afford a real page visit for
        # every one of these at discovery time.
        full_description = None
        detail_scraped_at = None
        detail_error = None
        if description and len(description) > 200:
            full_description = description
            detail_scraped_at = now
            if looks_expired(description):
                detail_error = "expired"

        # Extract apply URL if JobSpy provided it. Some rows have
        # job_url_direct as a genuine Python None (not pandas NaN) -- blindly
        # stringifying with str() before checking turns that into the literal
        # text "None", which passes any "!= 'nan'" check and gets stored as a
        # real (wrong) value instead of SQL NULL. Check the raw value first.
        raw_apply_url = row.get("job_url_direct")
        apply_url = None
        if raw_apply_url is not None:
            candidate = str(raw_apply_url).strip()
            if candidate and candidate.lower() not in ("nan", "none", "null"):
                apply_url = candidate

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, company, salary, description, location, site, strategy, discovered_at, "
                "full_description, application_url, detail_scraped_at, detail_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, title, company, salary, description, location_str, site_label, strategy, now,
                 full_description, apply_url, detail_scraped_at, detail_error),
            )
            new += 1
            if dedup_key:
                seen_keys.add(dedup_key)
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


# -- Single search execution -------------------------------------------------

def _run_one_search(
    search: dict,
    sites: list[str],
    results_per_site: int,
    hours_old: int,
    proxy_config: dict | None,
    defaults: dict,
    max_retries: int,
    accept_locs: list[str],
    reject_locs: list[str],
    glassdoor_map: dict,
) -> dict:
    """Run a single search query and store results in DB."""
    s = search
    label = f"\"{s['query']}\" in {s['location']} {'(remote)' if s.get('remote') else ''}"
    if "tier" in s:
        label += f" [tier {s['tier']}]"

    # Split sites: Glassdoor needs simplified location, others use original
    gd_location = glassdoor_map.get(s["location"], s["location"].split(",")[0])
    has_glassdoor = "glassdoor" in sites
    other_sites = [si for si in sites if si != "glassdoor"]

    all_dfs = []

    # Run non-Glassdoor sites with original location
    if other_sites:
        kwargs = {
            "site_name": other_sites,
            "search_term": s["query"],
            "location": s["location"],
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "country_indeed": defaults.get("country_indeed", "usa"),
            "verbose": 0,
        }
        if s.get("distance"):
            kwargs["distance"] = s["distance"]
        if s.get("remote"):
            kwargs["is_remote"] = True
        if proxy_config:
            kwargs["proxies"] = [proxy_config["jobspy"]]
        if "linkedin" in other_sites:
            kwargs["linkedin_fetch_description"] = True
        try:
            df = _scrape_with_retry(kwargs, max_retries=max_retries)
            all_dfs.append(df)
        except Exception as e:
            log.error("[%s] (non-gd): %s", label, e)

    # Run Glassdoor separately with simplified location
    if has_glassdoor:
        gd_kwargs = {
            "site_name": ["glassdoor"],
            "search_term": s["query"],
            "location": gd_location,
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "verbose": 0,
        }
        if s.get("remote"):
            gd_kwargs["is_remote"] = True
        if proxy_config:
            gd_kwargs["proxies"] = [proxy_config["jobspy"]]
        try:
            gd_df = _scrape_with_retry(gd_kwargs, max_retries=max_retries)
            all_dfs.append(gd_df)
        except Exception as e:
            log.error("[%s] (glassdoor): %s", label, e)

    if not all_dfs:
        log.error("[%s]: all sites failed", label)
        return {"new": 0, "existing": 0, "errors": 1, "filtered": 0, "total": 0, "label": label}

    import pandas as pd
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat(all_dfs, ignore_index=True) if len(all_dfs) > 1 else all_dfs[0]

    if len(df) == 0:
        log.info("[%s] 0 results", label)
        return {"new": 0, "existing": 0, "errors": 0, "filtered": 0, "total": 0, "label": label}

    # Filter by location before storing
    before = len(df)
    df = df[df.apply(lambda row: _location_ok(
        str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None,
        accept_locs, reject_locs,
    ), axis=1)]
    filtered = before - len(df)

    # Entry-level prefilter: drop senior/lead/director-level LinkedIn postings,
    # plus "Manager"-titled postings at major consulting firms (a senior,
    # experienced-hire grade there specifically -- see scorer.py).
    before_senior = len(df)
    if "site" in df.columns:
        df = df[~df.apply(
            lambda row: str(row.get("site", "")).lower() == "linkedin"
            and (
                is_senior_title(str(row.get("title", "")))
                or is_consulting_manager_title(str(row.get("title", "")), str(row.get("company", "")))
            ),
            axis=1,
        )]
    senior_filtered = before_senior - len(df)

    # Safety-net recency filter: LinkedIn's own f_TPR server-side filter isn't
    # always exact (renewed/reposted listings can slip through), so also check
    # the actual date_posted JobSpy returns. date_posted is day-granularity, so
    # allow a 1-day buffer to avoid rejecting jobs posted earlier today.
    stale = 0
    if hours_old and "date_posted" in df.columns:
        import math
        from datetime import date, timedelta
        cutoff = date.today() - timedelta(days=math.ceil(hours_old / 24) + 1)
        before_recency = len(df)
        df = df[df["date_posted"].isna() | (df["date_posted"] >= cutoff)]
        stale = before_recency - len(df)

    conn = get_connection()
    new, existing = store_jobspy_results(conn, df, s["query"])

    msg = f"[{label}] {before} results -> {new} new, {existing} dupes"
    if filtered:
        msg += f", {filtered} filtered (location)"
    if senior_filtered:
        msg += f", {senior_filtered} filtered (senior title, LinkedIn)"
    if stale:
        msg += f", {stale} filtered (stale, >{hours_old}h old)"
    log.info(msg)

    return {"new": new, "existing": existing, "errors": 0, "filtered": filtered + senior_filtered, "total": before, "label": label}


# -- Single query search -----------------------------------------------------

def search_jobs(
    query: str,
    location: str,
    sites: list[str] | None = None,
    remote_only: bool = False,
    results_per_site: int = 50,
    hours_old: int = 72,
    proxy: str | None = None,
    country_indeed: str = "usa",
) -> dict:
    """Run a single job search via JobSpy and store results in DB."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Search: \"%s\" in %s | sites=%s | remote=%s", query, location, sites, remote_only)

    kwargs = {
        "site_name": sites,
        "search_term": query,
        "location": location,
        "results_wanted": results_per_site,
        "hours_old": hours_old,
        "description_format": "markdown",
        "country_indeed": country_indeed,
        "verbose": 2,
    }

    if remote_only:
        kwargs["is_remote"] = True

    if proxy_config:
        kwargs["proxies"] = [proxy_config["jobspy"]]

    if "linkedin" in sites:
        kwargs["linkedin_fetch_description"] = True

    try:
        df = scrape_jobs(**kwargs)
    except Exception as e:
        log.error("JobSpy search failed: %s", e)
        return {"error": str(e), "total": 0, "new": 0, "existing": 0}

    total = len(df)
    log.info("JobSpy returned %d results", total)

    if total == 0:
        return {"total": 0, "new": 0, "existing": 0}

    if "site" in df.columns:
        site_counts = df["site"].value_counts()
        for site, count in site_counts.items():
            log.info("  %s: %d", site, count)

    conn = init_db()
    new, existing = store_jobspy_results(conn, df, query)
    log.info("Stored: %d new, %d already in DB", new, existing)

    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL").fetchone()[0]
    log.info("DB total: %d jobs, %d pending detail scrape", db_total, pending)

    return {"total": total, "new": new, "existing": existing}


# -- Region expansion (cities x roles) ---------------------------------------

def _normalize_searches(cfg: dict) -> list[dict]:
    """Expand `regions:` (cities x roles) and normalize `searches:` into one
    flat list of per-search dicts, each with a resolved country_indeed code
    and accept_locs list ready for _run_one_search.
    """
    default_hours_old = cfg.get("defaults", {}).get("hours_old", 72)
    normalized: list[dict] = []

    for region in cfg.get("regions", []):
        # `enabled: false` pauses a region without deleting its config, so
        # reactivating it later is a one-line change. Paused regions must
        # stay in sync with scoring.region_quota's paused buckets -- there's
        # no point crawling a region whose jobs can never be selected.
        if not region.get("enabled", True):
            log.info("Region '%s' is paused (enabled: false) -- skipping its searches.",
                     region.get("name", "?"))
            continue

        cities = region.get("cities", [])
        roles = region.get("roles", [])
        if not cities or not roles:
            continue

        # Accept locations span the whole region (a result near one region
        # city should still be accepted even if it was found via a search
        # centered on a different city in the same region).
        country_code, _ = _infer_region(cities[0])
        accept_locs: set[str] = set()
        for city in cities:
            accept_locs.add(city.split(",")[0].strip().lower())
            _, city_accept = _infer_region(city)
            accept_locs.update(city_accept)

        for city in cities:
            for role in roles:
                normalized.append({
                    "search_term": role,
                    "location": city,
                    "distance": region.get("distance", 50),
                    "hours_old": region.get("hours_old", default_hours_old),
                    "results_wanted": region.get("results_wanted", 8),
                    "site_name": region.get("site_name", ["linkedin", "indeed"]),
                    "country_indeed": region.get("country_indeed", country_code),
                    "accept_locs": sorted(accept_locs),
                })

    for s in cfg.get("searches", []):
        location = s.get("location", "")
        country_code, accept_locs = _infer_region(location)
        normalized.append({
            "search_term": s["search_term"],
            "location": location,
            "distance": s.get("distance"),
            "hours_old": s.get("hours_old", default_hours_old),
            "results_wanted": s.get("results_wanted", 20),
            "site_name": s.get("site_name", ["linkedin", "indeed"]),
            "country_indeed": s.get("country_indeed", country_code),
            "accept_locs": accept_locs,
            "is_remote": s.get("is_remote", False),
        })

    return normalized


# -- Full crawl (all queries x all locations) --------------------------------

def _full_crawl(
    search_cfg: dict,
    tiers: list[int] | None = None,
    locations: list[str] | None = None,
    sites: list[str] | None = None,
    results_per_site: int = 100,
    hours_old: int = 72,
    proxy: str | None = None,
    max_retries: int = 2,
) -> dict:
    """Run all search queries from search config across all locations."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    # Build search combinations from config
    queries = search_cfg.get("queries", [])
    locs = search_cfg.get("locations", [])
    defaults = search_cfg.get("defaults", {})
    glassdoor_map = search_cfg.get("glassdoor_location_map", {})
    accept_locs, reject_locs = _load_location_config(search_cfg)

    if tiers:
        queries = [q for q in queries if q.get("tier") in tiers]
    if locations:
        locs = [loc for loc in locs if loc.get("label") in locations]

    searches = []
    for q in queries:
        for loc in locs:
            searches.append({
                "query": q["query"],
                "location": loc["location"],
                "remote": loc.get("remote", False),
                "tier": q.get("tier", 0),
            })

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Full crawl: %d search combinations", len(searches))
    log.info("Sites: %s | Results/site: %d | Hours old: %d",
             ", ".join(sites), results_per_site, hours_old)

    # Ensure DB schema is ready
    init_db()

    total_new = 0
    total_existing = 0
    total_errors = 0
    completed = 0

    for s in searches:
        result = _run_one_search(
            s, sites, results_per_site, hours_old,
            proxy_config, defaults, max_retries,
            accept_locs, reject_locs, glassdoor_map,
        )
        completed += 1
        total_new += result["new"]
        total_existing += result["existing"]
        total_errors += result["errors"]

        if completed % 5 == 0 or completed == len(searches):
            log.info("Progress: %d/%d queries done (%d new, %d dupes, %d errors)",
                     completed, len(searches), total_new, total_existing, total_errors)

    # Final stats
    conn = get_connection()
    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    log.info("Full crawl complete: %d new | %d dupes | %d errors | %d total in DB",
             total_new, total_existing, total_errors, db_total)

    return {
        "new": total_new,
        "existing": total_existing,
        "errors": total_errors,
        "db_total": db_total,
        "queries": len(searches),
    }


# -- Public entry point ------------------------------------------------------

def run_discovery(cfg: dict | None = None) -> dict:
    if cfg is None:
        cfg = config.load_search_config()

    if not cfg:
        log.warning("No search configuration found. Run `applypilot init` to create one.")
        return {"new": 0, "existing": 0, "errors": 0, "db_total": 0, "queries": 0}

    # Support the `regions:` format (cities x roles, auto-expanded) and the
    # flat `searches:` format (one explicit location per entry). Both may be
    # present at once; results are combined into one normalized list.
    if "regions" in cfg or "searches" in cfg:
        normalized = _normalize_searches(cfg)
        proxy = cfg.get("proxy")
        proxy_config = parse_proxy(proxy) if proxy else None
        init_db()
        total_new = total_existing = total_errors = 0

        for s in normalized:
            search = {
                "query": s["search_term"],
                "location": s["location"],
                "remote": s.get("is_remote", False),
                "distance": s.get("distance"),
            }
            defaults = {"country_indeed": s["country_indeed"]}
            result = _run_one_search(
                search, s["site_name"], s["results_wanted"], s["hours_old"],
                proxy_config, defaults, 2, s["accept_locs"], [], {},
            )
            total_new += result["new"]
            total_existing += result["existing"]
            total_errors += result["errors"]

        conn = get_connection()
        db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        log.info("Full crawl complete: %d new | %d dupes | %d errors | %d total in DB (%d searches run)",
                 total_new, total_existing, total_errors, db_total, len(normalized))
        return {"new": total_new, "existing": total_existing,
                "errors": total_errors, "db_total": db_total}

    # Legacy format fallback
    proxy = cfg.get("proxy")
    sites = cfg.get("sites")
    results_per_site = cfg.get("defaults", {}).get("results_per_site", 100)
    hours_old = cfg.get("defaults", {}).get("hours_old", 72)
    tiers = cfg.get("tiers")
    locations = cfg.get("location_labels")

    return _full_crawl(
        search_cfg=cfg,
        tiers=tiers,
        locations=locations,
        sites=sites,
        results_per_site=results_per_site,
        hours_old=hours_old,
        proxy=proxy,
    )