"""Economic news feed: outlet-restricted GDELT queries + title-level topics.

Regression source: CI run 36747513137 (2026-09-30) published 12 headlines of
which none was economic news in a language the owner reads — Chinese, Greek,
Arabic, Serbian and Ukrainian articles, and a Brazilian music festival under
"dazi". GDELT matches queries against machine translations, so the query a row
came from is not evidence of what it is about.
"""
from __future__ import annotations

import pandas as pd
import pytest

from forwardguidex.ingest import news as newsmod
from forwardguidex.serve import snapshot as S
from forwardguidex.transform import news_topics as T

# ---------- classify ----------

@pytest.mark.parametrize("title,topic", [
    ("Fed's Powell says the committee can wait before cutting again", "banche_centrali"),
    ("ECB holds rates as Lagarde flags upside risks", "banche_centrali"),
    ("La Bce lascia i tassi invariati", "banche_centrali"),
    ("US core CPI eases to 2.9%, reinforcing disinflation trend", "inflazione"),
    ("Payrolls beat forecasts as unemployment rate holds at 4.1%", "lavoro"),
    ("New tariff round on imports rattles trade-exposed sectors", "commercio"),
    ("OPEC+ holds output steady ahead of demand review", "energia"),
    ("Treasury yields climb as deficit worries resurface", "debito_bond"),
    ("Euro-zone PMI signals manufacturing contraction", "crescita"),
    ("Istat conferma la crescita del Pil italiano", "crescita"),
])
def test_classify_economic_titles(title, topic):
    assert T.classify(title) == topic


@pytest.mark.parametrize("title", [
    # verbatim from the CI run 36747513137 snapshot
    "年内举牌8次 ！ 险资二级市场举牌降温",  # noqa: RUF001 - verbatim CJK punctuation
    "Χρηματιστήρια : Κλείσιμο με νέα υψηλά 17 ετών στην Αθήνα",
    "Festival Psica lança line - up com Zeca Pagodinho , Alceu Valença e mais",
    "Ministros israelíes califican como acto de  terrorismo  incidente en avión",
    # plain non-economic English
    "Champions League: late goal sends holders through",
    "",
    None,
])
def test_classify_rejects_non_economic_titles(title):
    assert T.classify(title) is None


def test_classify_needs_whole_words():
    # "fed" inside "confederation", "oil" inside "turmoil", "gas" in "Vegas"
    assert T.classify("Swiss confederation elects a new president") is None
    assert T.classify("Las Vegas casino reopens") is None
    assert T.classify("Political turmoil in the capital") is None


def test_specific_topics_win_over_the_catch_all():
    # names both inflation and "economy": the specific topic is the useful label
    assert T.classify("Inflation cools, giving the economy room") == "inflazione"


def test_tidy_title_undoes_gdelt_tokenisation_only():
    assert T.tidy_title("Trading down : How prices dictate  demand") == "Trading down: How prices dictate demand"
    assert T.tidy_title("U.S . economy adds jobs ( again )") == "U.S. economy adds jobs (again)"
    assert T.tidy_title("Rates rise 3 %") == "Rates rise 3%"
    assert T.tidy_title(None) == ""


def test_dedupe_key_ignores_case_and_punctuation():
    assert T.dedupe_key("Fed holds rates.") == T.dedupe_key("FED HOLDS RATES")


# ---------- queries ----------

# Run 36786024797 (2026-09-30): queries of ~500 chars (terms + a 15-outlet
# `domainis:` group) came back "Your query was too short or too long". The
# limit is undocumented; the queries GDELT accepted before were <= ~100 chars.
MAX_QUERY_CHARS = 120


def test_configured_queries_are_short_and_english_only():
    from forwardguidex import config
    uni = config.load_universe()
    queries = uni["gdelt_queries"]
    assert any(q.get("macro") for q in queries)
    assert len(queries) <= 5, "GDELT throttles per client: every query is another chance of a 429"
    for q in queries:
        assert len(q["query"]) <= MAX_QUERY_CHARS, (q["key"], len(q["query"]))
        assert "sourcelang:english" in q["query"], q["key"]
        assert "domainis:" not in q["query"], "outlets are preferred downstream, not in the query"


def test_trusted_outlets_are_configured():
    from forwardguidex import config
    trusted = config.load_universe()["news_trusted_domains"]
    assert {"reuters.com", "ft.com", "bloomberg.com", "cnbc.com"} <= set(trusted)


def test_ingest_sends_the_configured_query_verbatim(monkeypatch):
    sent = []

    class Client:
        def fetch_json(self, url, *, params=None, **_kw):
            sent.append(params["query"])
            from forwardguidex.ingest import http_client as httpc
            return httpc.FetchResult(ok=True, status=200, error_class=httpc.ErrorClass.OK,
                                     error_detail="", attempts=1, rate_limited_attempts=0,
                                     elapsed=0.0, data={"articles": []})

        def close(self):
            pass

    monkeypatch.setattr(newsmod, "HttpClient", Client)
    monkeypatch.setattr(newsmod, "load_universe", lambda: {
        "news_trusted_domains": ["reuters.com", "ft.com"],
        "gdelt_queries": [{"key": "k", "query": "(inflation OR CPI) sourcelang:english"}],
    })
    monkeypatch.setattr(newsmod, "_persist_health", lambda con, ts, r: None)
    newsmod.ingest_news_with_report(con=None)
    assert sent == ["(inflation OR CPI) sourcelang:english"]


@pytest.mark.parametrize("domain,url,ok", [
    ("reuters.com", "https://www.reuters.com/x", True),
    ("uk.reuters.com", "https://uk.reuters.com/x", True),
    ("www.ft.com", "https://www.ft.com/x", True),
    ("notreuters.com", "https://notreuters.com/x", False),
    ("microsoft.com", "https://microsoft.com/x", False),   # not "ft.com"
    ("", "https://markets.ft.com/x", True),                # URL-host fallback
    (None, None, False),
])
def test_is_trusted(domain, url, ok):
    assert T.is_trusted(domain, url, ["reuters.com", "ft.com"]) is ok


# ---------- snapshot feed ----------

def _rows(*rows):
    return pd.DataFrame(rows, columns=["topic", "title", "domain", "url", "seendate"])


def _uni(monkeypatch, trusted=None):
    monkeypatch.setattr(S.config, "load_universe", lambda: {
        "gdelt_queries": [
            {"key": "macro_dati", "macro": True, "query": "q1"},
            {"key": "banche_centrali", "macro": True, "query": "q2"},
            {"key": "geopolitica", "macro": False, "query": "q3"},
        ],
        "news_trusted_domains": trusted or [],
    })


def test_feed_labels_by_title_and_drops_non_economic(monkeypatch):
    _uni(monkeypatch)
    out = S._economic_headlines(_rows(
        ("macro_dati", "US core CPI eases to 2.9%", "reuters.com", "https://r/1", "20260930T160000Z"),
        ("macro_dati", "Festival lança line - up", "g1.globo.com", "https://g/1", "20260930T150000Z"),
        ("banche_centrali", "Fed holds rates steady", "wsj.com", "https://w/1", "20260930T140000Z"),
    ))
    assert [(h["topic"], h["url"]) for h in out] == [
        ("inflazione", "https://r/1"), ("banche_centrali", "https://w/1")]


def test_feed_excludes_geopolitical_queries_and_http(monkeypatch):
    _uni(monkeypatch)
    out = S._economic_headlines(_rows(
        ("geopolitica", "Sanctions hit Iran's oil exports", "ft.com", "https://f/1", "20260930T160000Z"),
        ("macro_dati", "Inflation jumps", "x.com", "http://insecure/1", "20260930T150000Z"),
    ))
    assert out == []


def test_feed_keeps_one_row_per_story(monkeypatch):
    _uni(monkeypatch)
    out = S._economic_headlines(_rows(
        ("macro_dati", "Fed holds rates steady", "wsj.com", "https://w/1", "20260930T160000Z"),
        # same URL through a second query
        ("banche_centrali", "Fed holds rates steady", "wsj.com", "https://w/1", "20260930T160000Z"),
        # same story, syndicated elsewhere with the same words
        ("banche_centrali", "FED holds rates steady.", "apnews.com", "https://a/1", "20260930T150000Z"),
    ))
    assert [h["url"] for h in out] == ["https://w/1"]


def test_feed_tidies_titles(monkeypatch):
    _uni(monkeypatch)
    out = S._economic_headlines(_rows(
        ("macro_dati", "Inflation : what the data say", "ft.com", "https://f/1", "20260930T160000Z"),
    ))
    assert out[0]["title"] == "Inflation: what the data say"


def test_demo_headlines_use_title_topics():
    for h in S.demo_snapshot()["headlines"]:
        assert T.classify(h["title"]) == h["topic"], h["title"]


def test_feed_prefers_trusted_outlets_and_fills_with_the_rest(monkeypatch):
    _uni(monkeypatch, trusted=["reuters.com"])
    monkeypatch.setattr(S, "NEWS_HEADLINE_CAP", 2)
    out = S._economic_headlines(_rows(
        # newest, but not trusted
        ("macro_dati", "Inflation jumps in Freedonia", "blog.example", "https://b/1", "20260930T180000Z"),
        ("macro_dati", "Payrolls beat forecasts", "reuters.com", "https://r/1", "20260930T160000Z"),
        ("banche_centrali", "Fed holds rates steady", "blog.example", "https://b/2", "20260930T150000Z"),
    ))
    urls = [h["url"] for h in out]
    assert "https://r/1" in urls               # the trusted one always makes it
    assert len(urls) == 2                      # the free slot is filled from the rest
    assert out == sorted(out, key=lambda h: h["seendate"], reverse=True)


def test_feed_is_all_trusted_when_there_are_enough(monkeypatch):
    _uni(monkeypatch, trusted=["reuters.com", "ft.com"])
    monkeypatch.setattr(S, "NEWS_HEADLINE_CAP", 2)
    out = S._economic_headlines(_rows(
        ("macro_dati", "Inflation jumps in Freedonia", "blog.example", "https://b/1", "20260930T180000Z"),
        ("macro_dati", "Payrolls beat forecasts", "reuters.com", "https://r/1", "20260930T160000Z"),
        ("banche_centrali", "Fed holds rates steady", "ft.com", "https://f/1", "20260930T150000Z"),
    ))
    assert {h["url"] for h in out} == {"https://r/1", "https://f/1"}
