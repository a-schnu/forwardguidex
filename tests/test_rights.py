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
