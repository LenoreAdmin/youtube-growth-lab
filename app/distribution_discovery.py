"""Auditable discovery intake for legitimate music submissions.

Publicly discovered contacts are leads, never permission to send.
Only documented official intake policies become eligible candidates.
"""
from dataclasses import dataclass
from urllib.parse import urlparse
from .distribution_policy import Submission, permitted

@dataclass(frozen=True)
class Lead:
    name: str
    homepage: str
    official_submission_url: str
    terms_evidence_url: str
    video_id: str
    explicitly_accepts_music: bool = False
    allows_automated_submission: bool = False
    free: bool = False
    relevant_music: bool = False
    no_rights_transfer: bool = False
    no_contract: bool = False
    no_new_upload: bool = False
    no_account_required: bool = False
    no_unwanted_contact: bool = False
    no_artificial_engagement: bool = False
    current_terms_checked: bool = False

def assess(lead: Lead) -> tuple[Submission | None, str]:
    """Convert a researched lead only when the rules explicitly permit automation."""
    if not lead.allows_automated_submission:
        return None, "Automated submissions not explicitly authorized."
    candidate = Submission(
        video_id=lead.video_id, destination_url=lead.homepage,
        official_submission_url=lead.official_submission_url,
        terms_evidence_url=lead.terms_evidence_url,
        explicit_permission=lead.explicitly_accepts_music and lead.allows_automated_submission,
        relevant_music=lead.relevant_music, free=lead.free,
        no_rights_transfer=lead.no_rights_transfer, no_contract=lead.no_contract,
        no_new_upload=lead.no_new_upload, no_account_required=lead.no_account_required,
        no_unwanted_contact=lead.no_unwanted_contact,
        no_artificial_engagement=lead.no_artificial_engagement,
        terms_verified=lead.current_terms_checked)
    ok, reason = permitted(candidate)
    return (candidate, "eligible") if ok else (None, reason)

def filter_leads(leads: list[Lead]) -> tuple[list[Submission], list[dict]]:
    eligible, rejected = [], []
    seen = set()
    for lead in leads:
        candidate, reason = assess(lead)
        key = (lead.official_submission_url, lead.video_id)
        if candidate is not None and key not in seen:
            eligible.append(candidate)
            seen.add(key)
        else:
            rejected.append({"name": lead.name, "video_id": lead.video_id,
                             "reason": reason if candidate is None else "duplicate"})
    return eligible, rejected
