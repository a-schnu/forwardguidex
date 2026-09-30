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


# ---------- compose_query ----------

def test_compose_query_appends_one_exact_host_group():
    q = newsmod.compose_query("(inflation OR CPI)", ["reuters.com", "FT.com", "reuters.com"])
    assert q == "(inflation OR CPI) (domainis:reuters.com OR domainis:ft.com)"


def test_compose_query_single_domain_is_not_parenthesised():
    # GDELT rejects a parenthesised group with a single term
    assert newsmod.compose_query("inflation", ["ft.com"]) == "inflation domainis:ft.com"


def test_compose_query_without_domains_is_unchanged():
    assert newsmod.compose_query("inflation", []) == "inflation"
    assert newsmod.compose_query("inflation", None) == "inflation"


def test_compose_query_drops_anything_that_is_not_a_bare_host():
    q = newsmod.compose_query("x", ["ok.com", "evil.com) OR (y", "sp ace.com", "noTLD", "a.b-c.it"])
    assert q == "x (domainis:ok.com OR domainis:a.b-c.it)"


def test_configured_queries_are_restricted_to_trusted_outlets():
    from forwardguidex import config
    uni = config.load_universe()
    domains = uni["gdelt_domains"]
    assert "reuters.com" in domains and "ilsole24ore.com" in domains
    for item in uni["gdelt_queries"]:
        q = newsmod.compose_query(item["query"], domains)
        assert q.count("domainis:") == len(set(domains)), item["key"]
    # the economic feed exists at all
    assert any(q.get("macro") for q in uni["gdelt_queries"])


def test_ingest_sends_the_restricted_query(monkeypatch):
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
        "gdelt_domains": ["reuters.com", "ft.com"],
        "gdelt_queries": [{"key": "k", "query": "(inflation OR CPI)"}],
    })
    monkeypatch.setattr(newsmod, "_persist_health", lambda con, ts, r: None)
    newsmod.ingest_news_with_report(con=None)
    assert sent == ["(inflation OR CPI) (domainis:reuters.com OR domainis:ft.com)"]


# ---------- snapshot feed ----------

def _rows(*rows):
    return pd.DataFrame(rows, columns=["topic", "title", "domain", "url", "seendate"])


def _uni(monkeypatch):
    monkeypatch.setattr(S.config, "load_universe", lambda: {"gdelt_queries": [
        {"key": "macro_dati", "macro": True, "query": "q1"},
        {"key": "banche_centrali", "macro": True, "query": "q2"},
        {"key": "geopolitica", "macro": False, "query": "q3"},
    ]})


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
