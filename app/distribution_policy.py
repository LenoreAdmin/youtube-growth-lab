"""Fail-closed gate for zero-cost, permission-based music submissions."""
from dataclasses import dataclass
from urllib.parse import urlparse

ALLOWED_VIDEO_IDS = frozenset({"Xb-tYP9_Ah4", "N9Lqh-tKY04", "Ppg00gw0MxE"})

@dataclass(frozen=True)
class Submission:
    video_id: str
    destination_url: str
    official_submission_url: str
    terms_evidence_url: str
    explicit_permission: bool = False
    relevant_music: bool = False
    free: bool = False
    no_rights_transfer: bool = False
    no_contract: bool = False
    no_new_upload: bool = False
    no_account_required: bool = False
    no_unwanted_contact: bool = False
    no_artificial_engagement: bool = False
    terms_verified: bool = False
    previously_contacted: bool = False

def permitted(item: Submission) -> tuple[bool, str]:
    if item.video_id not in ALLOWED_VIDEO_IDS:
        return False, "Only existing Sealand videos are eligible."
    urls = (item.destination_url, item.official_submission_url, item.terms_evidence_url)
    if not all(urlparse(u).scheme == "https" and urlparse(u).hostname for u in urls):
        return False, "Official HTTPS destination and verifiable terms required."
    if urlparse(item.destination_url).hostname != urlparse(item.official_submission_url).hostname:
        return False, "Submission endpoint must belong to destination."
    for name in ("explicit_permission", "relevant_music", "free", "no_rights_transfer",
                 "no_contract", "no_new_upload", "no_account_required", "no_unwanted_contact",
                 "no_artificial_engagement", "terms_verified"):
        if getattr(item, name) is not True:
            return False, f"Requirement unproven: {name}."
    if item.previously_contacted:
        return False, "Duplicate outreach prohibited."
    return True, "Eligible to submit; not proof of placement or reach."
