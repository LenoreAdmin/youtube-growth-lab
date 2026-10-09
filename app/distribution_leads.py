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
                 "verify", "Official free form accepts YouTube URLs, but requires contact email and artist context; automated form submission not authorized."),
    ResearchLead("Right Chord Music", "https://www.rightchordmusic.com/submit-music",
                 "exclude", "Official free form requires contact email, artist photo and bio; published music must also be on Spotify or Bandcamp; automated submission not authorized."),
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
