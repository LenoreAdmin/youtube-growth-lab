"""Permission-based outreach execution, deliberately fail-closed.

Only provider-specific, explicitly authorized transports may be registered.
A discovered URL alone is NEVER a sendable submission endpoint.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from .distribution_policy import Submission, permitted

VIDEO_NAMES = {
    "Xb-tYP9_Ah4": "Trainstories",
    "N9Lqh-tKY04": "Shine On",
    "Ppg00gw0MxE": "11AM Album - Teaser",
}
VIDEO_URL = "https://www.youtube.com/watch?v={}"

@dataclass(frozen=True)
class DeliveryReceipt:
    provider: str
    submission_id: str
    submitted_at: str
    video_id: str
    endpoint: str
    status: str = "submitted_not_placed"

def submission_message(candidate: Submission) -> dict:
    ok, reason = permitted(candidate)
    if not ok:
        raise ValueError(reason)
    return {
        "artist": "Sealand",
        "video_title": VIDEO_NAMES[candidate.video_id],
        "video_url": VIDEO_URL.format(candidate.video_id),
        "message": ("Hello, we are Sealand, an acoustic pop rock band from Switzerland. "
                    "We would like to submit this existing music video for editorial consideration. "
                    "No payment or promotion exchange is requested."),
    }

def submit(candidate: Submission, *, provider: str,
           authorized_transports: dict[str, Callable[[str, dict], str]],
           previously_submitted: set[tuple[str, str]]) -> DeliveryReceipt:
    """Submit once via a verified provider integration; never use generic email or POST."""
    payload = submission_message(candidate)
    if (candidate.official_submission_url, candidate.video_id) in previously_submitted:
        raise ValueError("This video has already been submitted to this endpoint.")
    transport = authorized_transports.get(provider)
    if transport is None:
        raise ValueError("No authorized provider-specific submission transport configured.")
    # Provider-specific adapters must independently validate current terms and
    # endpoint ownership before network delivery; generic web forms are not supported.
    # Require an explicit, provider-owned transport with independent policy verification.
    # A generic callable supplied by discovery is not proof of authorization.
    if not getattr(transport, "verified_provider_adapter", False):
        raise ValueError("Transport lacks verified provider authorization.")
    if not getattr(transport, "checks_live_terms", False):
        raise ValueError("Transport must re-check live terms before delivery.")
    external_id = transport(candidate.official_submission_url, payload)
    if not isinstance(external_id, str) or not external_id.strip():
        raise ValueError("Provider did not acknowledge submission; do not claim delivery.")
    return DeliveryReceipt(provider, external_id.strip(),
                           datetime.now(timezone.utc).isoformat(),
                           candidate.video_id, candidate.official_submission_url)
