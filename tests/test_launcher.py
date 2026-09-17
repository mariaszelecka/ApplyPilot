"""Tests for the failure-permanence classification that decides two things
at once: whether acquire_job() will auto-retry a failed job, and what the
apply-run notification email tells you to do about it (notify/digest.py
imports this same function so the two can't disagree).
"""

from applypilot.apply.launcher import _is_permanent_failure


def test_known_permanent_reasons():
    # A human has to act on these -- must never be auto-retried.
    for reason in ("login_issue", "captcha", "expired", "not_eligible_location"):
        assert _is_permanent_failure(reason) is True


def test_known_transient_reasons():
    # Infra flakiness -- safe to retry automatically.
    for reason in ("no_browser_tooling", "page_error", "timeout"):
        assert _is_permanent_failure(reason) is False


def test_prefix_match_catches_variants_not_in_the_exact_set():
    # PERMANENT_PREFIXES exists so a specific variant like
    # "cloudflare_blocked_us" or "blocked_by_akamai" is still caught even
    # though the exact string isn't in PERMANENT_FAILURES.
    assert _is_permanent_failure("cloudflare_blocked_us") is True
    assert _is_permanent_failure("blocked_by_akamai") is True


def test_result_with_reason_suffix():
    # Real call sites pass the full "RESULT:FAILED:<reason>"-style string
    # in some paths -- the function splits on ":" and checks the tail too.
    assert _is_permanent_failure("failed:login_issue") is True


def test_unclassified_reason_defaults_to_retryable():
    # A brand-new failure string nobody's classified yet is treated as
    # transient (the safer default -- see the dossier for why this is a
    # known, accepted trade-off rather than an oversight).
    assert _is_permanent_failure("some_never_seen_error") is False
