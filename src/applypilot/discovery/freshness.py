"""Shared listing-freshness heuristics.

Used both by detail.py (real page-visit enrichment) and jobspy.py (the
description JobSpy already hands back at discovery time) so a listing that's
already closed gets caught regardless of which path found it.
"""

# Phrases indicating a listing is dead (closed/filled/expired). English +
# German (Swiss postings are often bilingual).
EXPIRY_PHRASES = (
    "no longer accepting applications",
    "position has been filled",
    "this job posting has expired",
    "this vacancy is no longer available",
    "applications are now closed",
    "job is no longer available",
    "posting has closed",
    "stelle wurde bereits besetzt",
    "diese stelle ist nicht mehr verfügbar",
    "bewerbungen werden nicht mehr angenommen",
    "diese stellenanzeige ist nicht mehr aktiv",
)


def looks_expired(text: str | None) -> bool:
    """Heuristic: does this job description indicate a closed/filled listing?"""
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in EXPIRY_PHRASES)
