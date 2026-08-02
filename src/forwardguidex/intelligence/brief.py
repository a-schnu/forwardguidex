"""Assemble warehouse data into an LLM-written Morning Brief."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from ..config import BRIEF_DIR, load_universe
from ..transform import events as fevents
from ..transform import marts
from .llm import chat

SYSTEM = (
    "You are ForwardGuidex, a sharp market-intelligence analyst. Write a crisp, "
    "scannable pre-open Morning Brief for a retail investor with a DUAL horizon: "
    "(a) active leveraged long/short trading AND (b) long-term buy-and-hold "
    "investing — across global equities (US, Europe, Asia), ETFs, rates and crypto. "
    "Address both horizons where relevant. Be factual, specific and data-driven: "
    "cite the actual moves and levels from the data. Never give personalized "
    "buy/sell advice or price targets — surface facts, moves and context, and flag "
    "what to watch. FORMAT: use short Markdown '##' section headings, **bold** for "
    "key figures/tickers, and tight bullet lists. Do NOT use tables, images or HTML "
    "(the renderer strips them); headings, bold/italic, bullets and blockquotes only. "
    "Write the ENTIRE brief in ITALIAN."
)


def _fmt_movers(df: pd.DataFrame, n: int = 6) -> str:
    df = df.dropna(subset=["ret_1d"])
    if df.empty:
        return ""
    up, down = df.nlargest(n, "ret_1d"), df.nsmallest(n, "ret_1d")

    def line(r) -> str:
        tag = r["sector_label"] or r["role"]
        return f"- {r['ticker']} ({tag}): {r['ret_1d']:+.2f}% -> {r['last_close']:.2f}"

    return ("Top gainers:\n" + "\n".join(line(r) for _, r in up.iterrows())
            + "\n\nTop losers:\n" + "\n".join(line(r) for _, r in down.iterrows()))


def build_context(con) -> str:
    lat, sec, rat, nws = (marts.latest(con), marts.sectors(con),
                          marts.rates(con), marts.news(con, limit=18))
    parts = ["## MARKET SNAPSHOT"]
    if not lat.empty:
        parts.append(_fmt_movers(lat))
        idx = lat[lat["role"] == "index"].dropna(subset=["ret_1d"])
        if not idx.empty:
            parts.append("\n## INDICES (1d %)\n" + "\n".join(
                f"- {r.name or r.ticker}: {r.ret_1d:+.2f}%" for r in idx.itertuples()))
        cry = lat[lat["role"] == "crypto"].dropna(subset=["ret_1d"])
        if not cry.empty:
            parts.append("\n## CRYPTO (1d %)\n" + "\n".join(
                f"- {r.name or r.ticker}: {r.ret_1d:+.2f}% -> {r.last_close:.2f}" for r in cry.itertuples()))
    if not sec.empty:
        parts.append("\n## SECTORS (avg 1d %)\n" + "\n".join(
            f"- {r.sector_label}: {r.avg_ret_1d:+.2f}%" for r in sec.itertuples()))
    if not rat.empty:
        parts.append("\n## RATES / YIELDS\n" + "\n".join(
            f"- {r.name}: {r.value:.2f} (chg {r.chg:+.2f})" for r in rat.itertuples()))
    if not nws.empty:
        parts.append("\n## HEADLINES\n" + "\n".join(
            f"- [{r.topic}] {r.title} ({r.domain})" for r in nws.itertuples()))

    # Phase-2 events: central-bank decisions, upcoming earnings, catalysts.
    uni = load_universe()
    now = datetime.now(timezone.utc)
    cb = fevents.cb_events(con, uni.get("cb_policy_rates", []))
    if cb:
        def _cbline(e):
            if e["direction"] == "hold":
                return f"- {e['bank']}: {e['rate']:.2f}% (invariato)"
            verb = "rialzo" if e["direction"] == "hike" else "taglio"
            return f"- {e['bank']}: {e['rate']:.2f}% ({verb} {e['change_bp']:+d}pb il {e['as_of']})"
        parts.append("\n## BANCHE CENTRALI (ultima decisione)\n"
                     + "\n".join(_cbline(e) for e in cb))
    earn = fevents.upcoming_earnings(con, uni, now, days=14, limit=12)
    if earn:
        def _eline(e):
            est = f" (EPS stim. {e['eps_estimate']})" if e.get("eps_estimate") is not None else ""
            return f"- {e['date']} {e['ticker']} ({e['sector'] or '-'}){est}"
        parts.append("\n## EARNINGS PROSSIMI\n" + "\n".join(_eline(e) for e in earn))
    trig = fevents.recent_triggers(con, limit=8)
    if trig:
        def _tline(t):
            tag = "8-K" if t["kind"] == "sec_8k" else "EO"
            return f"- [{tag}] {t['date']} {t['title']}"
        parts.append("\n## CATALIZZATORI (ordini esecutivi & 8-K)\n"
                     + "\n".join(_tline(t) for t in trig))
    return "\n".join(parts)


def build_brief(con, save: bool = True) -> str:
    context = build_context(con)
    user = (
        "Write today's Morning Brief from this data, entirely in ITALIAN. Open with "
        "ONE bold line: a market-regime read (Risk-on / Risk-off / Mixed) plus a "
        "one-clause reason. Then EXACTLY these two '##' sections with these Italian "
        "headings:\n"
        "## Sintesi del giorno - 4-6 bullets that synthesise the day: the main index "
        "moves (USA / Europa / Asia), the best and worst sectors, rates and central "
        "banks, and any standout single-stock or crypto move. Cite the actual "
        "figures from the data.\n"
        "## Cosa tenere d'occhio - split into '**Breve termine (trading)**' and "
        "'**Lungo termine (investimento)**', 2-3 bullets each.\n"
        "Do NOT add a cross-asset section or any other section. Keep it tight and "
        "scannable, under ~450 words.\n\n"
        f"DATA:\n{context}"
    )
    body = chat([{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": user}])
    content = f"# ForwardGuidex - Morning Brief - {date.today():%Y-%m-%d}\n\n{body}"
    if save:
        BRIEF_DIR.mkdir(parents=True, exist_ok=True)
        (BRIEF_DIR / f"{date.today():%Y-%m-%d}.md").write_text(content, encoding="utf-8")
        _save_history(con, content)
    return content


def _save_history(con, content: str) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS brief_history (created_at TIMESTAMP, content VARCHAR)")
    con.execute("INSERT INTO brief_history VALUES (?, ?)",
                [datetime.now(timezone.utc).replace(tzinfo=None), content])
