"""Source-rights gate: per-source, per-use, per-mode enforcement."""
import copy

import pytest

from forwardguidex import config
from forwardguidex.serve import rights


@pytest.fixture
def snap():
    return {
        "indices": [{"ticker": "^GSPC", "source": "yfinance"}],
        "sectors": [{"key": "energia", "etfs": [{"source": "yfinance"}],
                     "constituents": [{"source": "yfinance"}]}],
        "rates": [{"series_id": "UST10Y", "source": "UST"},
                  {"series_id": "EFFR", "source": "NYFED"}],
        "headlines": [{"title": "x"}],
    }


def test_sources_detected(snap):
    assert rights.sources_in_snapshot(snap) == {"yfinance", "us_treasury", "ny_fed", "gdelt"}


@pytest.fixture
def snap_events():
    """Snapshot carrying only the Phase-2 event sections + their sources."""
    return {
        "cb_events": [{"bank": "Fed", "series_id": "USFED", "direction": "hold",
                       "source": "BIS"}],
        "earnings": [{"ticker": "AAPL", "date": "2026-08-05", "source": "yfinance"}],
        "triggers": [
            {"kind": "executive_order", "title": "x", "date": "2026-07-20",
             "url": "https://www.federalregister.gov/documents/x", "source": "federal_register"},
            {"kind": "sec_8k", "title": "NVDA — 8-K", "date": "2026-07-30",
             "url": "https://www.sec.gov/Archives/edgar/data/1045810/x.htm", "source": "sec_edgar"},
        ],
    }


def test_event_sources_detected(snap_events):
    assert rights.sources_in_snapshot(snap_events) == {
        "bis", "yfinance", "federal_register", "sec_edgar"}


def test_event_sources_pass_private_personal(snap_events):
    assert rights.enforce("PRIVATE_PERSONAL", snapshot=snap_events) == []


def test_new_source_attribution_present(snap_events):
    keys = rights.sources_in_snapshot(snap_events)
    attr = rights.attribution_block(keys)
    assert attr["federal_register"].startswith("Source: U.S. Federal Register")
    assert attr["sec_edgar"].startswith("Source: U.S. Securities and Exchange Commission")


def test_private_personal_passes(snap):
    assert rights.enforce("PRIVATE_PERSONAL", snapshot=snap) == []


def test_public_commercial_rejects(snap):
    # yfinance/ny_fed/gdelt are private-only
    assert rights.enforce("PUBLIC_COMMERCIAL", snapshot=snap)


def test_unknown_mode_rejected(snap):
    v = rights.enforce("NOPE", snapshot=snap)
    assert v and "unknown deployment_mode" in v[0].reason


def test_expired_review_rejected(snap):
    pol = copy.deepcopy(config.load_sources())
    pol["sources"]["us_treasury"]["review_expires_at"] = "2020-01-01"
    v = rights.enforce("PRIVATE_PERSONAL", snapshot=snap, policy=pol)
    assert any("expired" in x.reason for x in v)


def test_pending_approval_rejected(snap):
    pol = copy.deepcopy(config.load_sources())
    pol["sources"]["ny_fed"]["approval_status"] = "pending"
    v = rights.enforce("PRIVATE_PERSONAL", snapshot=snap, policy=pol)
    assert any(x.source == "ny_fed" and "not approved" in x.reason for x in v)


def test_missing_evidence_rejected(snap):
    pol = copy.deepcopy(config.load_sources())
    pol["sources"]["yfinance"]["evidence_reference"] = ""
    v = rights.enforce("PRIVATE_PERSONAL", snapshot=snap, policy=pol)
    assert any(x.source == "yfinance" and "evidence" in x.reason for x in v)


def test_use_not_allowed_rejected(snap):
    pol = copy.deepcopy(config.load_sources())
    pol["sources"]["ny_fed"]["allowed_uses"] = ["persistence"]  # drop dashboard/ai/telegram
    v = rights.enforce("PRIVATE_PERSONAL", snapshot=snap, policy=pol)
    assert any(x.source == "ny_fed" and "not in allowed_uses" in x.reason for x in v)


def test_check_raises(snap):
    pol = copy.deepcopy(config.load_sources())
    pol["sources"]["yfinance"]["approval_status"] = "rejected"
    with pytest.raises(rights.RightsError):
        rights.check("PRIVATE_PERSONAL", snapshot=snap, policy=pol)


def test_attribution_block():
    att = rights.attribution_block({"us_treasury", "ny_fed"})
    assert att["us_treasury"].startswith("Source: U.S. Department")
    assert att["ny_fed"]  # non-empty disclaimer text
    assert "bis" not in att  # only present when BIS data is in the snapshot


def test_bis_attribution_block():
    att = rights.attribution_block({"bis"})
    assert att["bis"].startswith("Source: Bank for International Settlements")


def test_nyfed_disclaimer_is_verbatim():
    # the sentinel/scaffolding must be gone, replaced with the real notice
    txt = rights.nyfed_disclaimer()
    assert "[[REPLACE" not in txt and "ACTION REQUIRED" not in txt
    assert txt.startswith("The EFFR and SOFR data is subject to the Terms of Use")


@pytest.fixture
def snap_bis():
    return {
        "indices": [{"ticker": "^GSPC", "source": "yfinance"}],
        "etfs": [{"ticker": "ILF", "source": "yfinance"}],
        "rates": [{"series_id": "BOEBR", "source": "BIS"}],
    }


def test_bis_source_detected(snap_bis):
    assert "bis" in rights.sources_in_snapshot(snap_bis)


def test_bis_private_personal_passes(snap_bis):
    assert rights.enforce("PRIVATE_PERSONAL", snapshot=snap_bis) == []


def test_bis_public_rejected(snap_bis):
    # BIS is private/personal only -> any PUBLIC_* mode must reject
    v = rights.enforce("PUBLIC_NONCOMMERCIAL", snapshot=snap_bis)
    assert any(x.source == "bis" for x in v)
