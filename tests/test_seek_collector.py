from pathlib import Path

import pytest

from app.collectors.base import AccessChallengeError
from app.collectors.seek import (
    build_seek_search_url,
    detect_genuine_access_challenge,
    detect_optional_signin_modal,
    detect_signed_in_session,
    extract_seek_job_id,
    fallback_key_for_job,
    inspect_genuine_access_challenge,
    is_browser_closed_error,
    parse_seek_detail_page,
    parse_seek_listing_page,
    validate_collectable_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_build_seek_search_url_with_date_and_page() -> None:
    url = build_seek_search_url("strategy analyst", "Sydney NSW", "last_3_days", page=2)
    assert url == "https://www.seek.com.au/strategy-analyst-jobs/in-Sydney-NSW?daterange=3&page=2"


def test_extract_seek_job_id_from_url() -> None:
    assert extract_seek_job_id("https://www.seek.com.au/job/12345678?type=standard") == "12345678"


def test_parse_listing_fixture() -> None:
    html = (FIXTURES / "seek_listing.html").read_text(encoding="utf-8")
    jobs = parse_seek_listing_page(html)
    assert len(jobs) == 1
    assert jobs[0].seek_job_id == "12345678"
    assert jobs[0].title == "Strategy Analyst"
    assert jobs[0].company == "Example Co"
    assert jobs[0].salary == "$110k - $130k"
    assert jobs[0].url == "https://www.seek.com.au/job/12345678?type=standard"
    assert jobs[0].card_type == "normal"
    assert jobs[0].parser_path == "seek:data-automation-job-article"
    assert jobs[0].rank == 1


def test_unknown_provenance_fallback() -> None:
    html = """
    <html><body>
      <div>
        <a href="/job/444">Strategy Analyst</a>
      </div>
    </body></html>
    """
    jobs = parse_seek_listing_page(html)
    assert len(jobs) == 1
    assert jobs[0].card_type == "unknown"
    assert jobs[0].parser_path == "seek:fallback-job-link-parent"


def test_parse_detail_fixture() -> None:
    html = (FIXTURES / "seek_detail.html").read_text(encoding="utf-8")
    detail = parse_seek_detail_page(html)
    assert detail["title"] == "Strategy Analyst"
    assert detail["company"] == "Example Co"
    assert "executive decisions" in detail["description"]


def test_fallback_key_is_deterministic() -> None:
    first = fallback_key_for_job(
        "Strategy Analyst", "Example Co", "Sydney NSW", "https://www.seek.com.au/job/123?x=1"
    )
    second = fallback_key_for_job(
        " strategy analyst ", "example co", "sydney nsw", "https://www.seek.com.au/job/123"
    )
    assert first == second


def test_ordinary_signin_modal_with_results_is_collectable() -> None:
    html = """
    <html><body>
      <div role="dialog"><h2>Sign in to save jobs</h2><button>Maybe later</button></div>
      <article data-automation="normalJob">
        <a data-automation="jobTitle" href="/job/222">Strategy Analyst</a>
        <span data-automation="jobCompany">Example Co</span>
      </article>
    </body></html>
    """
    assert detect_optional_signin_modal(html)
    assert detect_genuine_access_challenge(html, "https://www.seek.com.au/jobs") is None
    validate_collectable_page(html, "https://www.seek.com.au/jobs")


def test_genuine_challenge_is_separate_from_login() -> None:
    html = "<html><body><h1>Security check</h1><p>Verify you are human</p></body></html>"
    assert detect_genuine_access_challenge(html, "https://www.seek.com.au/jobs")
    detection = inspect_genuine_access_challenge(
        html, "https://www.seek.com.au/jobs", "Security check"
    )
    assert detection is not None
    assert detection.rule == "phrase:verify-you-are-human"
    with pytest.raises(AccessChallengeError):
        validate_collectable_page(html, "https://www.seek.com.au/jobs")


def test_prepared_signed_in_session_indicator() -> None:
    html = """
    <html><body>
      <button data-automation="profile-menu">Account</button>
      <a href="/saved-jobs">Saved jobs</a>
    </body></html>
    """
    assert detect_signed_in_session(html)


def test_signed_in_results_page_is_not_a_challenge() -> None:
    html = """
    <html><head><title>Strategy analyst jobs</title></head><body>
      <button data-automation="profile-menu">Account</button>
      <a href="/saved-jobs">Saved jobs</a>
      <article data-automation="normalJob">
        <a data-automation="jobTitle" href="/job/333">Strategy Analyst</a>
        <span>Viewed</span>
      </article>
    </body></html>
    """
    assert inspect_genuine_access_challenge(html, "https://www.seek.com.au/jobs") is None
    validate_collectable_page(html, "https://www.seek.com.au/jobs", "Strategy analyst jobs")


def test_manual_browser_closure_is_detected() -> None:
    assert is_browser_closed_error(
        RuntimeError("Page.wait_for_timeout: Target page, context or browser has been closed")
    )
