"""PRODUKTREGEL: bestehende Beziehungen werden gemessen, nie angesprochen.

Radios, Redaktionen, Medien, Kuratoren und einbettende Seiten, die unsere Musik selbst ausgewaehlt haben,
sind gewachsene Beziehungen. Dieses System darf sie nicht anschreiben, nicht fuer Experimente benutzen und
auch nicht durch Lernen wieder freischalten. Ihre Trafficdaten tragen das Audience-Lernen – nichts weiter.
Im Zweifel wird nicht kontaktiert.
"""
from datetime import timedelta
import pytest
from sqlalchemy import select
from app import acquisition as aq
from app.models import (AudiencePool, DiscoveryChannel, DiscoveryItem, DiscoverySignal, GrowthAction,
                        TrafficSurface, Video, VideoProfile)
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG
from test_acquisition import FakeHttp, signal, own_theme, new_audience_channel
from test_pools import pool


def test_a_radio_station_that_already_plays_us_is_never_contacted(session):
    signal(session, "a", "own_external", "radiosender.example/playlist", 2500)
    aq.collect(session, NOW, http=FakeHttp())
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "own_external_referrer"))
    assert surface is not None, "die Quelle bleibt als Messsignal sichtbar"
    assert surface.access["actionable"] is False and surface.access["protected"] is True
    assert "2500 Views" in surface.access["why_not"] and "nie angesprochen" in surface.access["why_not"]
    assert aq.propose(session, NOW)["proposed"] == 0
    # Auch das Lernen darf die Sperre nicht aufheben: sie steckt im Zugang, nicht im Hebelgewicht.
    assert surface.scores["expected_weekly_views"] > 0, "die Daten fliessen weiter in die Messung"


def test_a_recommending_channel_is_a_measurement_signal_not_a_target(session):
    session.add(DiscoveryItem(video_id="NB000000001", channel_id="UCR", title="Radiosendung Folge 12",
                              channel_title="Radio Beispiel", views=500000, tags=[],
                              via={"suggested_source": ["own_traffic"]}, first_seen_day=TODAY, last_seen_day=TODAY,
                              seen_count=1))
    session.add(DiscoveryChannel(channel_id="UCR", title="Radio Beispiel", subscribers=250000, video_count=900,
                                 views=90000000, first_seen_day=TODAY, last_seen_day=TODAY))
    signal(session, "a", "own_suggested_source", "NB000000001", 40)
    session.commit()
    aq.collect(session, NOW, http=FakeHttp())
    for surface in session.scalars(select(TrafficSurface)):
        assert surface.access["actionable"] is False and surface.access.get("protected") is True
    assert aq.propose(session, NOW)["proposed"] == 0


def test_a_newly_found_channel_that_already_sends_us_traffic_is_protected(session):
    """Derselbe Kanal kann in der Suche auftauchen – er bleibt trotzdem eine bestehende Quelle."""
    own_theme(session)
    session.add(DiscoveryItem(video_id="NB000000002", channel_id="UCnew", title="Ambient night train radio hour",
                              channel_title="Night Ambient Radio", views=300000, tags=[],
                              via={"suggested_source": ["own_traffic"]}, first_seen_day=TODAY, last_seen_day=TODAY,
                              seen_count=1))
    signal(session, "a", "own_suggested_source", "NB000000002", 25)
    new_audience_channel(session)
    result = aq.collect(session, NOW, http=FakeHttp())
    verdict = next(v for v in result["pools"]["verdicts"] if v["title"] == "Night Ambient Radio")
    assert verdict["kept"] is False and "bestehende Quelle" in verdict["reason"]
    assert not list(session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "pool_channel")))


def test_a_playlist_that_already_holds_our_music_is_protected(session):
    own_theme(session)
    pool(session, "playlist", "PLmine", "Night train ambient journeys", item_count=40, subscribers=50000,
         channel_title="Slow Travel Sounds", description="ambient night train journey",
         details={"contains_own": True})
    result = aq.collect(session, NOW, http=FakeHttp())
    verdict = next(v for v in result["pools"]["verdicts"] if v["title"] == "Night train ambient journeys")
    assert verdict["kept"] is False
    assert "fuehrt unsere Musik bereits" in verdict["reason"] and "bestehende Platzierung" in verdict["reason"]


def test_an_unverified_playlist_is_not_contacted_either(session):
    """Im Zweifel nicht kontaktieren: solange die Mitgliedschaft offen ist, bleibt die Playlist unberuehrt."""
    own_theme(session)
    pool(session, "playlist", "PLunknown", "Night train ambient journeys", item_count=40, subscribers=50000,
         channel_title="Slow Travel Sounds", description="ambient night train journey",
         details={"contains_own": None})
    result = aq.collect(session, NOW, http=FakeHttp())
    verdict = next(v for v in result["pools"]["verdicts"] if v["title"] == "Night train ambient journeys")
    assert verdict["kept"] is False and "im Zweifel nicht kontaktieren" in verdict["reason"]


def test_the_learning_loop_cannot_re_enable_a_protected_source(session):
    """Selbst wenn ein Hebel als erfolgreich gelernt wird, bleibt die Sperre bestehen."""
    signal(session, "a", "own_external", "radiosender.example", 5000)
    aq.collect(session, NOW, http=FakeHttp())
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "own_external_referrer"))
    assert surface.access["protected"] is True
    # Der Zugang wird vor jeder Gewichtung entschieden: kein Gewicht kann ihn umdrehen.
    assert "protected" in aq.referrer_access("https://radiosender.example", 5000)
    assert aq.referrer_access("https://radiosender.example", 5000)["actionable"] is False


def test_the_dashboard_shows_protected_sources_as_measurement(session):
    signal(session, "a", "own_external", "radiosender.example", 900)
    aq.collect(session, NOW, http=FakeHttp())
    view = aq.overview(session, NOW)
    assert view["traffic_queue"] == []
    entry = next(x for x in view["protected_sources"] if "radiosender" in (x["title"] or ""))
    assert entry["measured_views_90d"] == 900 and "nie angesprochen" in entry["why_not"]
    assert any("nie angesprochen" in x for x in view["capabilities"]["not_used"])
    from pathlib import Path
    assert 'id="trafficProtected"' in Path("app/static/index.html").read_text(encoding="utf-8")
    assert "protected_sources" in Path("app/static/app.js").read_text(encoding="utf-8")
