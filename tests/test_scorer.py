"""Pure-function tests for the deterministic dealbreaker filters in scorer.py.

No network, no database, no LLM call -- these run in milliseconds and catch
the class of bug that actually happened in production: a title that should
have matched a filter didn't (UBS/Julius Baer graduate-scheme postings
reaching the digest), or a filter that should NOT match something matched it
anyway (a false positive on "graduate degree preferred").
"""

from applypilot.scoring.scorer import (
    is_consulting_manager_title,
    is_graduate_program_title,
    is_senior_title,
    is_ubs_internship_title,
)

# -- is_senior_title ----------------------------------------------------

def test_senior_title_matches_plain_keyword():
    assert is_senior_title("Senior Product Manager") is True


def test_senior_title_matches_through_punctuation():
    # \b word-boundary matching, not a hardcoded "senior " substring --
    # this is exactly the case a naive substring check misses.
    assert is_senior_title("(Senior) Consultant") is True
    assert is_senior_title("Manager, Senior") is True


def test_senior_title_does_not_match_unrelated_titles():
    assert is_senior_title("Product Manager") is False
    assert is_senior_title("Associate Consultant") is False


def test_senior_title_handles_missing_title():
    assert is_senior_title(None) is False
    assert is_senior_title("") is False


# -- is_graduate_program_title -------------------------------------------

def test_graduate_program_matches_known_phrases():
    assert is_graduate_program_title("UBS Graduate Talent Program 2027") is True
    assert is_graduate_program_title("University Graduate - Investment Bank") is True


def test_graduate_program_does_not_false_positive_on_bare_word():
    # The dangerous case: "graduate" appears in a completely normal
    # requirement, not a graduate scheme. Must NOT match.
    assert is_graduate_program_title("Product Manager (graduate degree preferred)") is False


def test_graduate_program_does_not_match_unrelated_titles():
    assert is_graduate_program_title("Senior Product Manager") is False


# -- is_consulting_manager_title -----------------------------------------

def test_consulting_manager_matches_at_named_firm():
    assert is_consulting_manager_title("Manager", "Deloitte") is True


def test_consulting_manager_does_not_match_manager_elsewhere():
    # "Manager" is a normal target title outside the named consulting
    # firms -- must not be excluded everywhere.
    assert is_consulting_manager_title("Operations Manager", "Acme Logistics") is False


def test_consulting_manager_requires_both_title_and_company():
    assert is_consulting_manager_title("Analyst", "Deloitte") is False
    assert is_consulting_manager_title("Manager", None) is False


# -- is_ubs_internship_title ----------------------------------------------

def test_ubs_internship_matches():
    assert is_ubs_internship_title("Intern - Wealth Management", "UBS") is True
    assert is_ubs_internship_title("Internship, Corporate Center", "UBS AG") is True


def test_ubs_internship_does_not_match_other_companies():
    assert is_ubs_internship_title("Business Analyst Intern", "Deloitte") is False


def test_ubs_internship_does_not_false_positive_on_substring():
    # "International" contains "intern" but is not the word "intern" --
    # word-boundary matching must reject it.
    assert is_ubs_internship_title("International Business Development", "UBS") is False
    # "ubs" as a substring inside another word must not match the company check.
    assert is_ubs_internship_title("Intern", "PubSub AG") is False
