"""Economic topic of a headline, read from its title.

The GDELT query a headline came from is a poor label: GDELT matches against
machine translations, so a query about tariffs returned a music festival under
"dazi" (CI run 36747513137). The dashboard therefore labels each headline by
what its *title* is about, and a title that names no economic topic at all is
not shown — this is the precision filter behind the "Notizie" section.

Rules are ordered: the first topic whose pattern matches wins, so the more
specific topics (central banks, inflation, jobs) sit before the catch-all
"crescita". Patterns cover English and Italian, the languages of the outlets in
``news_trusted_domains``.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# (key, pattern). Keys are what the snapshot publishes as `headline.topic`; the
# dashboard maps them to Italian labels.
_RULES: tuple[tuple[str, str], ...] = (
    ("banche_centrali",
     r"fed|federal reserve|fomc|powell|ecb|bce|european central bank|banca centrale"
     r"|lagarde|bank of england|boe|bank of japan|boj|pboc|central banks?|banche centrali"
     r"|rate cuts?|rate hikes?|interest rates?|tassi|monetary policy|politica monetaria"),
    ("inflazione",
     r"inflation(?:ary)?|disinflation|deflation|cpi|pce|hicp|ppi|consumer prices?"
     r"|producer prices?|price pressures?|inflazione|prezzi al consumo|carovita"),
    ("lavoro",
     r"jobs? report|jobs data|payrolls?|nonfarm|unemployment|jobless|labou?r market"
     r"|wages?|hiring|layoffs?|employment|occupazione|disoccupazione|lavoro|salari"),
    ("commercio",
     r"tariffs?|trade (?:war|deal|talks|deficit|surplus|tensions?|policy)|exports?"
     r"|imports?|customs|protectionism|dazi|commercio"),
    ("energia",
     r"oil|crude|brent|wti|opec\+?|natural gas|lng|gasoline|energy prices?|petrolio"
     r"|greggio|gas"),
    ("debito_bond",
     r"treasur(?:y|ies)|bonds?|yields?|gilts?|bunds?|btps?|deficits?|debt|budget|fiscal"
     r"|debito|bilancio|manovra|rendimenti|spread btp"),
    ("crescita",
     r"gdp|recession|economy|economic|economies|growth|pmi|manufacturing|factory"
     r"|retail sales|consumer (?:spending|confidence|sentiment)|housing market"
     r"|pil|recessione|crescita|economia|industria|consumi"),
)

_COMPILED = tuple(
    (key, re.compile(r"(?<![\w-])(?:" + pat + r")(?![\w-])", re.IGNORECASE))
    for key, pat in _RULES
)

TOPICS: tuple[str, ...] = tuple(key for key, _ in _RULES)


def classify(title: str | None) -> str | None:
    """Return the first matching topic key, or None for a non-economic title."""
    if not title:
        return None
    for key, rx in _COMPILED:
        if rx.search(title):
            return key
    return None


# GDELT tokenises titles, which leaves a space before punctuation
# ("Trading down : How prices ...", "U.S . economy"). Undo just that.
_SPACE_BEFORE = re.compile(r"\s+([:;,.!?%)\]])")
_SPACE_AFTER = re.compile(r"([(\[])\s+")
_MULTI_SPACE = re.compile(r"\s{2,}")


def tidy_title(title: str | None) -> str:
    """Collapse GDELT's tokenisation artefacts; never changes the words."""
    if not title:
        return ""
    t = _SPACE_BEFORE.sub(r"\1", title)
    t = _SPACE_AFTER.sub(r"\1", t)
    return _MULTI_SPACE.sub(" ", t).strip()


def dedupe_key(title: str | None) -> str:
    """Identity of a story across outlets and queries: its words, lower-cased."""
    return " ".join(re.findall(r"\w+", (title or "").lower()))


def is_trusted(domain: str | None, url: str | None, trusted: list[str]) -> bool:
    """True when the headline's host is a trusted outlet or a subdomain of one.

    Uses GDELT's ``domain`` field, falling back to the URL host. Suffix match on
    a dot boundary, so ``uk.reuters.com`` counts and ``notreuters.com`` does not.
    """
    host = (domain or "").strip().lower()
    if not host and url:
        host = (urlsplit(url).hostname or "").lower()
    host = host.removeprefix("www.")
    if not host:
        return False
    for d in trusted or []:
        d = str(d).strip().lower()
        if d and (host == d or host.endswith("." + d)):
            return True
    return False
