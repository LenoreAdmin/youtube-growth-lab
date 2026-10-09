"""Researched distribution leads (2026-10-09). Not permission to submit.

Never automatically contact any of these outlets until their CURRENT official
terms expressly permit the proposed automated transport, with no account,
payment, contract, or rights transfer.
"""
from dataclasses import dataclass

@dataclass(frozen=True)
class ResearchLead:
    name: str
    source_url: str
    status: str
    reason: str

LEADS = (
    ResearchLead("ATC Sound", "https://blog.atcsound.com/indie-artists-free-music-submission/",
                 "verify", "Official form accepts YouTube links; free use, current terms and automation permission unverified."),
    ResearchLead("Right Chord Music", "https://www.rightchordmusic.com/",
                 "verify", "Free submissions advertised; official submission endpoint and automation terms unverified."),
    ResearchLead("Indie Rock Cafe", "https://www.indierockcafe.com/labels/About%20IRC.html",
                 "exclude", "Requests explicit permission to post music and other requirements; no automated consent."),
    ResearchLead("Groover", "https://groover.co/en/",
                 "exclude", "Standard curator submissions cost money."),
    ResearchLead("SubmitHub", "https://www.submithub.com/about/terms",
                 "exclude", "Free credits exist but account and authorization for automated use not established."),
)

def candidates():
    """Research only; cannot return an authorized Submission or trigger delivery."""
    return [lead for lead in LEADS if lead.status == "verify"]
