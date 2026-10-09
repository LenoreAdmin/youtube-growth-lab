"""Regression tests for permission-based distribution. No network or delivery."""
import pytest
from app.distribution_discovery import Lead, assess, filter_leads
from app.distribution import submit

BASE = dict(
    name="Editorial outlet", homepage="https://example.org/",
    official_submission_url="https://example.org/submit",
    terms_evidence_url="https://example.org/terms",
    video_id="Xb-tYP9_Ah4",
    explicitly_accepts_music=True, allows_automated_submission=True,
    free=True, relevant_music=True, no_rights_transfer=True,
    no_contract=True, no_new_upload=True, no_account_required=True,
    no_unwanted_contact=True, no_artificial_engagement=True,
    current_terms_checked=True,
)

def test_unproven_automation_permission_rejected():
    candidate, reason = assess(Lead(**{**BASE, "allows_automated_submission": False}))
    assert candidate is None
    assert "not explicitly authorized" in reason

def test_unverified_terms_rejected():
    candidate, reason = assess(Lead(**{**BASE, "current_terms_checked": False}))
    assert candidate is None
    assert "terms_verified" in reason

def test_only_existing_videos():
    candidate, reason = assess(Lead(**{**BASE, "video_id": "some-new-upload"}))
    assert candidate is None
    assert "Only existing" in reason

def test_duplicate_leads_filtered():
    eligible, rejected = filter_leads([Lead(**BASE), Lead(**BASE)])
    assert len(eligible) == 1
    assert rejected[0]["reason"] == "duplicate"

def test_no_generic_submission_transport():
    candidate, reason = assess(Lead(**BASE))
    assert candidate is not None, reason
    with pytest.raises(ValueError, match="No authorized"):
        submit(candidate, provider="outlet", authorized_transports={},
               previously_submitted=set())

def test_unverified_transport_cannot_send():
    candidate, _ = assess(Lead(**BASE))
    called = []
    def unsafe(url, payload):
        called.append(url)
        return "fake-id"
    with pytest.raises(ValueError, match="verified provider"):
        submit(candidate, provider="outlet", authorized_transports={"outlet": unsafe},
               previously_submitted=set())
    assert called == []
