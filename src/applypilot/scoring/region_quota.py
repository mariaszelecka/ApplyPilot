"""Geographic quota selection for the tailoring pipeline.

CURRENT SCOPE: Switzerland only, split 75/25 between German-speaking
Switzerland and the Geneva/Lausanne area -- for the standard limit=20,
that's 15 + 5. This is a configured focus, not a hard limit of the code.

Dublin, France/Monaco, and US/Silicon Valley are PAUSED, not deleted --
their bucket definitions, keyword matching (still sourced from
profile.json -> job_preferences.region_rules, so they stay in sync with
the region-rule restrictions used at scoring time), and quota-splitting
code are all still here, just set to 0 in `_ABROAD_QUOTA_SHARE` below. To
resume searching those regions, restore their share there -- nothing else
needs to change.

Bucket matching is NOT a separate, broader set of country/city keywords --
it reuses the exact same location patterns already defined in
profile.json -> job_preferences.region_rules (for the still-active abroad
buckets) or a dedicated Swiss-canton split (for the two active buckets),
so quota selection and the region-rule restriction enforced at scoring time
never drift apart.

If a bucket doesn't have enough qualifying (already-scored, fresh,
untailored) jobs on a given day, that bucket is simply under-filled --
never padded with lower-quality matches just to hit a number.
"""

from applypilot.config import load_profile

# German-speaking Switzerland: everything except the Geneva/Lausanne area
# named explicitly below. "switzerland" alone (no specific city) defaults
# here too, since German-speaking cantons cover most of the country/jobs.
SWISS_GERMAN_KEYWORDS = (
    "zurich", "zürich", "zug", "winterthur", "basel", "bern", "st. gallen",
    "st.gallen", "luzern", "lucerne", "aarau", "schaffhausen", "solothurn",
    "chur", "thun", "biel", "bienne", "switzerland",
)
# Scoped to the Geneva/Lausanne area specifically, not the whole of
# French-speaking Switzerland (Romandy).
SWISS_FRENCH_KEYWORDS = (
    "geneva", "genève", "geneve", "lausanne", "vaud", "nyon", "montreux",
)

# Italian-speaking Ticino. Excluded outright: not commutable from Zurich and
# not in the acceptable-onsite list, so a Ticino posting could only ever be
# matched, tailored, then refused at the apply step. It slips in without this
# because SWISS_GERMAN_KEYWORDS carries a bare "switzerland" catch-all that
# "<town>, Ticino, Switzerland" also matches.
SWISS_EXCLUDED_KEYWORDS = (
    "ticino", "lugano", "bioggio", "chiasso", "bellinzona", "locarno",
    "mendrisio", "manno", "paradiso",
)

BUCKETS = ("swiss_german", "swiss_french", "dublin", "france", "us")

# Maps profile.json's region_rules keys to the (paused) abroad buckets.
_REGION_RULE_TO_BUCKET = {
    "Ireland": "dublin",
    "France/Monaco": "france",
    "United States": "us",
}

# Fraction of `limit` each bucket gets. Must sum to 1.0. Dublin/France/US
# are PAUSED (0.0) per explicit instruction -- see module docstring. To
# resume abroad search, give them back a nonzero share (and reduce the
# Swiss shares accordingly so this still sums to 1.0).
_QUOTA_SHARE: dict[str, float] = {
    "swiss_german": 0.75,
    "swiss_french": 0.25,
    "dublin": 0.0,
    "france": 0.0,
    "us": 0.0,
}


def _load_abroad_keywords() -> dict[str, list[str]]:
    """Pull the (currently paused) abroad buckets' match patterns from
    profile.json -> job_preferences.region_rules -- kept live so reactivating
    them later is just a quota-share change, not a re-implementation."""
    profile = load_profile()
    region_rules = profile.get("job_preferences", {}).get("region_rules", {})
    keywords: dict[str, list[str]] = {}
    for region_name, bucket in _REGION_RULE_TO_BUCKET.items():
        cfg = region_rules.get(region_name, {})
        keywords[bucket] = [k.lower() for k in cfg.get("match", [])]
    return keywords


def bucket_for(location: str | None, abroad_keywords: dict[str, list[str]] | None = None) -> str | None:
    """Classify a job's location into one quota bucket, or None if it
    doesn't fall in the target scope (e.g. UK/London -- the UK has its own
    region_rule for scoring purposes, but no dedicated quota bucket here).

    Swiss-French cities are checked before the generic "switzerland" catch-
    all in SWISS_GERMAN_KEYWORDS, so a Geneva/Lausanne posting that also
    happens to mention "Switzerland" elsewhere in its location string still
    lands in the right bucket.

    `abroad_keywords` lets callers processing many jobs (e.g.
    select_with_quota) load profile.json once and reuse it, instead of
    reloading per job. Standalone callers can omit it.
    """
    loc = (location or "").lower()

    # Checked before anything else: an excluded region must not fall through
    # to the "switzerland" catch-all below.
    if any(k in loc for k in SWISS_EXCLUDED_KEYWORDS):
        return None

    if any(k in loc for k in SWISS_FRENCH_KEYWORDS):
        return "swiss_french"
    if any(k in loc for k in SWISS_GERMAN_KEYWORDS):
        return "swiss_german"

    if abroad_keywords is None:
        abroad_keywords = _load_abroad_keywords()
    for bucket, keywords in abroad_keywords.items():
        if any(k in loc for k in keywords):
            return bucket
    return None


def quota_for(limit: int) -> dict[str, int]:
    """Compute per-bucket quotas for a given total batch size, from
    _QUOTA_SHARE. Any rounding remainder is absorbed by swiss_german so the
    buckets always sum to exactly `limit`.
    """
    quotas = {b: round(limit * _QUOTA_SHARE[b]) for b in BUCKETS if b != "swiss_german"}
    quotas["swiss_german"] = limit - sum(quotas.values())
    return {b: quotas[b] for b in BUCKETS}


def select_with_quota(candidates: list[dict], limit: int) -> list[dict]:
    """Select up to `limit` jobs from `candidates`, respecting the regional
    quota split. `candidates` must already be ordered best-first (e.g. by
    fit_score DESC) -- each bucket takes its top N in that order. Jobs
    outside the target buckets (e.g. UK/London) are never selected.
    Under-filled buckets are NOT padded from elsewhere.
    """
    quotas = quota_for(limit)
    abroad_keywords = _load_abroad_keywords()
    buckets: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    for job in candidates:
        b = bucket_for(job.get("location"), abroad_keywords)
        if b is not None:
            buckets[b].append(job)

    selected: list[dict] = []
    for b, quota in quotas.items():
        selected.extend(buckets[b][:quota])
    return selected
