/* ForwardGuidex — dashboard renderer.
 *
 * Security posture:
 *   - Every scalar value from the snapshot is written via textContent (never innerHTML).
 *   - The ONLY innerHTML assignment here is the Morning Brief, and only on DOMPurify-
 *     sanitised output of marked.parse(). External links go through setSafeExternalLink
 *     (https-only). Chat replies follow the same rule inside chat.js.
 *   - marked / DOMPurify are self-hosted globals loaded before this module.
 */

import { loadSnapshot, marketStateCosmetic } from './data.js';
import {
  el, isNum, arr, fmtPrice, fmtNum, fmtPct, fmtBp, fmtRate, fmtDate, fmtDay, fmtDateTime, signClass,
  pctChip, segmented, flipReorder, makeSafeLink, setSafeExternalLink, parseSeendate, relTime, onWidthChange, REDUCE
} from './util.js';
import { sparkline, yieldCurveChart, magnitudeBar } from './charts.js';
import { wireChat, setChatSnapshot, setViewContext } from './chat.js';
import { initDrawer, openInstrument, openSector, openBrief, drawerContext } from './drawer.js';
import { initPalette } from './palette.js';

const $ = (id) => document.getElementById(id);

/* ---------- labels ---------- */

/** Cosmetic ticker -> region map for grouping indices; unmapped falls into "Altri". */
const REGION_BY_TICKER = {
  '^GSPC': 'Americhe', '^NDX': 'Americhe', '^DJI': 'Americhe', '^RUT': 'Americhe',
  '^STOXX': 'Europa', '^STOXX50E': 'Europa', '^GDAXI': 'Europa', '^FCHI': 'Europa',
  '^FTSE': 'Europa', 'FTSEMIB.MI': 'Europa',
  '^N225': 'Asia', '^HSI': 'Asia', '000001.SS': 'Asia', '^KS11': 'Asia'
};
const REGION_ORDER = ['Americhe', 'Europa', 'Asia', 'Altri'];

/** Title-derived economic topics (serve/snapshot.py `_economic_headlines`). */
const TOPIC_LABELS = {
  banche_centrali: 'Banche centrali', inflazione: 'Inflazione', lavoro: 'Lavoro',
  commercio: 'Commercio', energia: 'Energia', debito_bond: 'Debito & bond', crescita: 'Crescita'
};
function topicLabel(k) {
  if (TOPIC_LABELS[k]) return TOPIC_LABELS[k];
  const s = String(k || '').replace(/_/g, ' ');
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : '—';
}

const OUTLETS = {
  'reuters.com': 'Reuters', 'bloomberg.com': 'Bloomberg', 'ft.com': 'Financial Times', 'wsj.com': 'WSJ',
  'cnbc.com': 'CNBC', 'marketwatch.com': 'MarketWatch', 'apnews.com': 'AP', 'economist.com': 'The Economist',
  'barrons.com': 'Barron\'s', 'nytimes.com': 'New York Times', 'theguardian.com': 'The Guardian',
  'bbc.co.uk': 'BBC', 'bbc.com': 'BBC', 'ilsole24ore.com': 'Il Sole 24 Ore', 'ansa.it': 'ANSA'
};
function outlet(domain) {
  const d = String(domain || '').replace(/^www\./, '');
  return OUTLETS[d] || d;
}

const TAB_DEFS = [
  { id: 'indici', label: 'Indici', key: 'indices', kind: 'Indice' },
  { id: 'futures', label: 'Futures & materie prime', key: 'futures', kind: 'Future' },
  { id: 'etf', label: 'ETF', key: 'etfs', kind: 'ETF' },
  { id: 'crypto', label: 'Crypto', key: 'crypto', kind: 'Crypto' }
];

/* ---------- registry ---------- */

const instruments = new Map();   // ticker -> { item, kind, sector }
const sectors = new Map();       // key -> sector

function register(item, kind, sectorKey) {
  if (!item || !item.ticker) return;
  const prev = instruments.get(item.ticker);
  if (!prev) {
    instruments.set(item.ticker, { item: item, kind: kind, sector: sectorKey || null });
    return;
  }
  if (!arr(prev.item.spark).length && arr(item.spark).length) prev.item = Object.assign({}, item, prev.item, { spark: item.spark });
  if (!prev.sector && sectorKey) prev.sector = sectorKey;
}

function buildRegistry(snap) {
  TAB_DEFS.forEach((t) => arr(snap[t.key]).forEach((it) => register(it, t.kind)));
  arr(snap.sectors).forEach((s) => {
    sectors.set(s.key, s);
    arr(s.etfs).forEach((it) => register(it, 'ETF', s.key));
    arr(s.constituents).forEach((it) => register(it, 'Azione', s.key));
  });
  const byLabel = new Map(arr(snap.sectors).map((s) => [s.label, s.key]));
  const mv = snap.movers || {};
  arr(mv.gainers).concat(arr(mv.losers)).forEach((it) => register(it, 'Azione', byLabel.get(it.sector)));
}

/* ---------- header: status + tape ---------- */

function renderStatus(meta) {
  const ms = marketStateCosmetic();
  const mkt = $('mktState');
  mkt.classList.toggle('open', ms.open);
  $('mktLabel').textContent = ms.open ? 'USA aperto' : 'USA chiuso';
  mkt.title = ms.label;

  const asof = $('asOf');
  // A date-only as-of arrives as midnight UTC; printing "00:00 UTC" would invent a time.
  const dateOnly = /T00:00(:00(\.0+)?)?(Z|[+-]00:?00)?$/.test(String(meta.data_as_of || ''));
  asof.textContent = 'Dati al ' + (dateOnly ? fmtDate(meta.data_as_of) : fmtDateTime(meta.data_as_of));
  if (meta.generated_at) asof.title = 'Snapshot generato ' + fmtDateTime(meta.generated_at);
  const issues = [];
  if (meta.freshness && meta.freshness !== 'FRESH') issues.push('dati ' + String(meta.freshness).toLowerCase());
  if (meta.quality && meta.quality !== 'OK') issues.push('qualità ' + String(meta.quality).toLowerCase());
  const q = $('quality');
  q.classList.toggle('warn', issues.length > 0);
  q.title = issues.length ? 'Attenzione: ' + issues.join(', ') + '. Vedi le note nelle sezioni.' : 'Dati completi e aggiornati';
  q.setAttribute('aria-label', q.title);
  if (meta.is_demo) $('demoTag').hidden = false;
}

const TAPE_FUTURES = ['CL=F', 'BZ=F', 'NG=F', 'GC=F', 'SI=F', 'HG=F', '6E=F', 'ZN=F'];

function renderTape(snap) {
  const track = $('tapeTrack');
  track.textContent = '';
  const futures = arr(snap.futures);
  const pick = arr(snap.indices)
    .concat(TAPE_FUTURES.map((t) => futures.find((f) => f.ticker === t)).filter(Boolean))
    .concat(arr(snap.crypto).slice(0, 3));
  if (!pick.length) { $('tape').hidden = true; return; }
  const tickers = pick.map((it) => it.ticker);
  const build = (clone) => {
    const g = el('div', 'tape-group');
    if (clone) g.setAttribute('aria-hidden', 'true');
    pick.forEach((it) => {
      const b = el('button', 'tape-item');
      b.type = 'button';
      b.setAttribute('data-tk', it.ticker);
      if (clone) b.tabIndex = -1;
      b.appendChild(el('span', 'tape-n', it.name || it.ticker));
      b.appendChild(el('span', 'tape-v num', fmtPrice(it.last)));
      b.appendChild(el('span', 'chg ' + signClass(it.ret_1d), fmtPct(it.ret_1d)));
      b.addEventListener('click', () => openInstrument(it.ticker, tickers));
      g.appendChild(b);
    });
    return g;
  };
  track.appendChild(build(false));
  track.appendChild(build(true));
  track.style.setProperty('--tape-dur', Math.max(40, pick.length * 3.2) + 's');
}

/* ---------- Morning Brief ---------- */

const BRIEF_SANITIZE = {
  ALLOWED_TAGS: ['h1', 'h2', 'h3', 'p', 'ul', 'ol', 'li', 'strong', 'em', 'blockquote', 'code', 'pre', 'a', 'br'],
  ALLOWED_ATTR: ['href', 'title'],
  ALLOW_DATA_ATTR: false,
  ALLOW_ARIA_ATTR: false
};

let briefNode = null;

/**
 * Sanitise the brief once. The page shows its lead + first bullets; the drawer
 * shows the whole thing (a clone of this sanitised DOM).
 */
function renderBrief(snap) {
  const strip = $('brief');
  const b = snap.brief || {};
  const md = typeof b.markdown === 'string' ? b.markdown.trim() : '';
  if (!md) { strip.hidden = true; return; }

  const node = el('div');
  // Only place innerHTML is used on this page, and only on sanitised output.
  node.innerHTML = window.DOMPurify.sanitize(window.marked.parse(md), BRIEF_SANITIZE);
  node.querySelectorAll('a').forEach(setSafeExternalLink);
  enhanceBrief(node);
  briefNode = node;

  // Page summary: the regime line and the first list, cloned from sanitised DOM.
  const lead = node.querySelector('.brief-regime') || node.querySelector('p');
  const leadHost = $('briefLead');
  leadHost.textContent = '';
  if (lead) {
    leadHost.textContent = lead.textContent.trim();
    const m = /brief-regime-(up|down|flat)/.exec(lead.className || '');
    leadHost.className = 'brief-lead tone-' + (m ? m[1] : 'flat');
  }
  const pts = $('briefPoints');
  pts.textContent = '';
  const firstList = node.querySelector('ul, ol');
  arr(firstList ? [].slice.call(firstList.children) : []).slice(0, 6).forEach((li) => {
    const c = li.cloneNode(true);
    c.classList.add('bp');
    pts.appendChild(c);
  });
  $('briefWhen').textContent = b.created_at ? fmtDateTime(b.created_at) : '';
  strip.hidden = false;
  const open = () => openBrief();
  $('briefOpen').addEventListener('click', open);
  pts.addEventListener('click', open);
}

/**
 * Class-only enrichment of the sanitised brief: drop the redundant H1 title
 * (the panel has its own header), mark the regime line and the two horizon
 * blocks. No new innerHTML, no icons.
 */
function enhanceBrief(node) {
  try {
    const h1 = node.firstElementChild;
    if (h1 && h1.tagName === 'H1' && /brief/i.test(h1.textContent || '')) h1.remove();
    const first = node.firstElementChild;
    if (first && first.tagName === 'P') {
      const t = (first.textContent || '').trim();
      const strong = first.querySelector('strong');
      const fullyBold = !!strong && (strong.textContent || '').trim().length >= t.length * 0.6;
      if (fullyBold || /regime|risk[- ]?o|rischio|propension|avversion/i.test(t)) {
        let sign = 'flat';
        if (/risk[- ]?on|propensione al rischio|propensione/i.test(t)) sign = 'up';
        else if (/risk[- ]?off|avversione al rischio|avversione/i.test(t)) sign = 'down';
        first.classList.add('brief-regime', 'brief-regime-' + sign);
      }
    }
    node.querySelectorAll('strong').forEach((s) => {
      const t = (s.textContent || '').toLowerCase().trim();
      let kind = null;
      if (/^breve\s*termine|^short[- ]?term/.test(t)) kind = 'short';
      else if (/^lungo\s*termine|^long[- ]?term/.test(t)) kind = 'long';
      if (!kind) return;
      const block = (s.closest && s.closest('p, li')) || s.parentElement;
      if (!block) return;
      block.classList.add('brief-' + kind);
      const next = block.nextElementSibling;
      if (next && (next.tagName === 'UL' || next.tagName === 'OL')) next.classList.add('brief-' + kind);
    });
  } catch (_e) { /* cosmetic only */ }
}

/* ---------- markets (tabs + tiles) ---------- */

let ccyMode = 'local';
let sortMode = 'default';
const tiles = [];            // { node, item }
const panels = [];           // { id, groups: [{ grid, tiles:[node], items:[item] }] }
let activeTab = null;

/* Index LEVELS are points, not prices: converting them to EUR (KOSPI "4,47 EUR")
 * means nothing, so the currency toggle applies to tradable quotes only. */
function tileValue(item, kind) {
  const eur = ccyMode === 'eur' && kind !== 'Indice' && item.currency !== 'EUR' && isNum(item.eur);
  return { last: eur ? item.eur : item.last, ccy: eur ? 'EUR' : (item.currency || '') };
}

function paintTile(t) {
  const v = tileValue(t.item, t.kind);
  t.last.textContent = fmtPrice(v.last);
  t.ccy.textContent = v.ccy;
  t.node.setAttribute('aria-label', (t.item.name || t.item.ticker) + ', ' + fmtPrice(v.last) + ' ' + v.ccy +
    ', ' + fmtPct(t.item.ret_1d) + ' oggi. Apri il dettaglio');
}

function tile(item, kind, listFn) {
  const b = el('button', 'tile');
  b.type = 'button';
  b.setAttribute('data-tk', item.ticker);
  const top = el('span', 't-top');
  top.appendChild(el('span', 't-name', item.name || item.ticker));
  top.appendChild(el('span', 'tk', item.ticker));
  b.appendChild(top);
  const px = el('span', 't-px');
  const last = el('span', 't-last num');
  const ccy = el('span', 't-ccy');
  px.appendChild(last);
  px.appendChild(ccy);
  b.appendChild(px);
  const chg = el('span', 't-chg');
  chg.appendChild(pctChip(item.ret_1d));
  chg.appendChild(el('span', 't-5d', '5g ' + fmtPct(item.ret_5d)));
  b.appendChild(chg);
  const sp = sparkline(item.spark, { w: 160, h: 34 });
  if (sp) {
    const holder = el('span', 't-spark');
    holder.appendChild(sp);
    b.appendChild(holder);
  }
  const t = { node: b, item: item, kind: kind, last: last, ccy: ccy };
  paintTile(t);
  tiles.push(t);
  b.addEventListener('click', () => openInstrument(item.ticker, listFn()));
  return b;
}

function sortedItems(items) {
  if (sortMode === 'default') return items.slice();
  return items.slice().sort((a, b) => (isNum(b[sortMode]) ? b[sortMode] : -1e9) - (isNum(a[sortMode]) ? a[sortMode] : -1e9));
}

function renderMarkets(snap) {
  const tablist = $('ovTabs');
  const host = $('ovPanels');
  tablist.textContent = '';
  host.textContent = '';
  const defs = TAB_DEFS.filter((t) => arr(snap[t.key]).length);
  if (!defs.length) { $('mercati').hidden = true; return; }

  const ccySeg = segmented('Valuta', [
    { value: 'local', label: 'Locale' },
    { value: 'eur', label: 'EUR', title: 'Prezzi convertiti in euro (non si applica ai livelli degli indici)' }
  ], ccyMode, (v) => { ccyMode = v; tiles.forEach(paintTile); });
  const btns = [];
  defs.forEach((t, i) => {
    const btn = el('button', 'tab');
    btn.type = 'button';
    btn.id = 'tab-' + t.id;
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-controls', 'panel-' + t.id);
    btn.setAttribute('aria-selected', i === 0 ? 'true' : 'false');
    btn.tabIndex = i === 0 ? 0 : -1;
    btn.appendChild(el('span', null, t.label));
    btn.appendChild(el('span', 'tab-n', String(arr(snap[t.key]).length)));
    tablist.appendChild(btn);
    btns.push(btn);

    const panel = el('div', 'panel-grid');
    panel.id = 'panel-' + t.id;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', btn.id);
    panel.hidden = i !== 0;

    const items = arr(snap[t.key]);
    const groups = [];
    const buckets = t.id === 'indici'
      ? REGION_ORDER.map((r) => [r, items.filter((it) => (REGION_BY_TICKER[it.ticker] || 'Altri') === r)])
      : [[null, items]];
    const rec = { id: t.id, label: t.label, groups: groups, panel: panel };
    buckets.forEach(([label, list]) => {
      if (!list.length) return;
      if (label) panel.appendChild(el('div', 'grp-l', label));
      const grid = el('div', 'tiles');
      const g = { grid: grid, items: list, nodes: new Map() };
      list.forEach((it) => {
        const n = tile(it, t.kind, () => visibleTickers(rec));
        g.nodes.set(it, n);
        grid.appendChild(n);
      });
      groups.push(g);
      panel.appendChild(grid);
    });
    panels.push(rec);
    host.appendChild(panel);
  });
  activeTab = panels[0];

  const select = (idx, focus) => {
    btns.forEach((b, k) => {
      const on = k === idx;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
      panels[k].panel.hidden = !on;
    });
    activeTab = panels[idx];
    ccySeg.node.hidden = activeTab.id === 'indici';
    if (focus) btns[idx].focus();
  };
  btns.forEach((b, i) => {
    b.addEventListener('click', () => select(i, false));
    b.addEventListener('keydown', (e) => {
      let ni = null;
      if (e.key === 'ArrowRight') ni = (i + 1) % btns.length;
      else if (e.key === 'ArrowLeft') ni = (i - 1 + btns.length) % btns.length;
      else if (e.key === 'Home') ni = 0;
      else if (e.key === 'End') ni = btns.length - 1;
      if (ni != null) { e.preventDefault(); select(ni, true); }
    });
  });

  const tools = $('mktTools');
  tools.textContent = '';
  tools.appendChild(segmented('Ordina per', [
    { value: 'default', label: 'Default' },
    { value: 'ret_1d', label: '1g', title: 'Ordina per variazione di oggi' },
    { value: 'ret_5d', label: '5g', title: 'Ordina per variazione a 5 giorni' }
  ], sortMode, (v) => { sortMode = v; applySort(); }).node);
  tools.appendChild(ccySeg.node);
  ccySeg.node.hidden = activeTab.id === 'indici';
}

function visibleTickers(rec) {
  const out = [];
  rec.groups.forEach((g) => sortedItems(g.items).forEach((it) => out.push(it.ticker)));
  return out;
}

function applySort() {
  panels.forEach((p) => p.groups.forEach((g) => {
    const order = sortedItems(g.items).map((it) => g.nodes.get(it));
    if (p.panel.hidden) order.forEach((n) => g.grid.appendChild(n));
    else flipReorder(g.grid, order);
  }));
}

/* ---------- movers ---------- */

function renderMovers(snap) {
  const mv = snap.movers || {};
  const gainers = arr(mv.gainers), losers = arr(mv.losers);
  if (!gainers.length && !losers.length) { $('movimenti').hidden = true; return; }
  const maxAbs = Math.max.apply(null, gainers.concat(losers).map((it) => Math.abs(it.ret_1d) || 0).concat(0.01));
  const all = gainers.concat(losers).map((it) => it.ticker);
  const fill = (host, list) => {
    host.textContent = '';
    list.forEach((it, i) => {
      const r = el('button', 'mrow');
      r.type = 'button';
      r.setAttribute('data-tk', it.ticker);
      r.appendChild(el('span', 'mrow-i num', String(i + 1)));
      r.appendChild(el('span', 'tk', it.ticker));
      const n = el('span', 'mrow-n');
      n.appendChild(el('span', 'mrow-name', it.name || ''));
      if (it.sector) n.appendChild(el('span', 'mrow-sec', it.sector));
      r.appendChild(n);
      const bar = el('span', 'mrow-b');
      bar.appendChild(magnitudeBar(it.ret_1d, maxAbs));
      r.appendChild(bar);
      r.appendChild(el('span', 'mrow-p num', fmtPrice(it.last)));
      r.appendChild(pctChip(it.ret_1d));
      r.addEventListener('click', () => openInstrument(it.ticker, all));
      host.appendChild(r);
    });
  };
  fill($('moversUp'), gainers);
  fill($('moversDown'), losers);
}

/* ---------- sectors (heatmap) ---------- */

let sectorPeriod = 'avg_ret_1d';

function renderSectors(snap) {
  const list = arr(snap.sectors);
  const host = $('heatmap');
  host.textContent = '';
  if (!list.length) { $('settori').hidden = true; return; }

  const cons = [];
  list.forEach((s) => arr(s.constituents).forEach((c) => cons.push(c)));
  const up = cons.filter((c) => isNum(c.ret_1d) && c.ret_1d > 0).length;
  const upSec = list.filter((s) => isNum(s.avg_ret_1d) && s.avg_ret_1d > 0).length;
  $('breadth').textContent = upSec + '/' + list.length + ' settori e ' + up + '/' + cons.length + ' titoli in rialzo oggi';

  const nodes = new Map();
  list.forEach((s) => {
    const t = el('button', 'hm');
    t.type = 'button';
    t.appendChild(el('span', 'hm-l', s.label));
    const big = el('span', 'hm-v num');
    const small = el('span', 'hm-s num');
    t.appendChild(big);
    t.appendChild(small);
    const ranked = arr(s.constituents).filter((c) => isNum(c.ret_1d)).slice().sort((a, b) => b.ret_1d - a.ret_1d);
    if (ranked.length) {
      const ex = el('span', 'hm-x');
      const best = ranked[0], worst = ranked[ranked.length - 1];
      const a = el('span');
      a.appendChild(el('span', 'tk', best.ticker));
      a.appendChild(el('span', 'chg ' + signClass(best.ret_1d), fmtPct(best.ret_1d)));
      ex.appendChild(a);
      if (worst !== best) {
        const z = el('span');
        z.appendChild(el('span', 'tk', worst.ticker));
        z.appendChild(el('span', 'chg ' + signClass(worst.ret_1d), fmtPct(worst.ret_1d)));
        ex.appendChild(z);
      }
      t.appendChild(ex);
    }
    t.addEventListener('click', () => openSector(s.key));
    nodes.set(s, { t: t, big: big, small: small });
    host.appendChild(t);
  });

  const paint = (animate) => {
    const key = sectorPeriod, other = key === 'avg_ret_1d' ? 'avg_ret_5d' : 'avg_ret_1d';
    const maxAbs = Math.max.apply(null, list.map((s) => Math.abs(s[key]) || 0).concat(0.25));
    list.forEach((s) => {
      const n = nodes.get(s);
      const v = s[key];
      n.big.textContent = fmtPct(v);
      n.small.textContent = (key === 'avg_ret_1d' ? '5g ' : '1g ') + fmtPct(s[other]);
      n.t.classList.remove('up', 'down', 'flat');
      n.t.classList.add(signClass(v));
      n.t.style.setProperty('--a', isNum(v) ? String(Math.min(1, Math.abs(v) / maxAbs)) : '0');
      n.t.setAttribute('aria-label', s.label + ' ' + fmtPct(v) + (key === 'avg_ret_1d' ? ' oggi' : ' a 5 giorni') + '. Apri il dettaglio');
    });
    const order = list.slice().sort((a, b) => (isNum(b[key]) ? b[key] : -1e9) - (isNum(a[key]) ? a[key] : -1e9))
      .map((s) => nodes.get(s).t);
    if (animate) flipReorder(host, order); else order.forEach((n) => host.appendChild(n));
  };
  paint(false);
  const tools = $('secTools');
  tools.textContent = '';
  tools.appendChild(segmented('Periodo', [
    { value: 'avg_ret_1d', label: '1g' }, { value: 'avg_ret_5d', label: '5g' }
  ], sectorPeriod, (v) => { sectorPeriod = v; paint(true); }).node);
}

/* ---------- rates ---------- */

function tenorYears(seriesId) {
  const m = /UST\s*(\d+(?:\.\d+)?)\s*(M|Y)/i.exec(seriesId || '');
  if (!m) return null;
  const n = parseFloat(m[1]);
  return m[2].toUpperCase() === 'M' ? n / 12 : n;
}
function tenorLabel(y) {
  return y < 1 ? Math.round(y * 12) + 'M' : (Number.isInteger(y) ? y : y.toFixed(1)) + 'A';
}

function spreadChip(label, a, b) {
  if (!a || !b) return null;
  const s = b.value - a.value;
  const d = isNum(a.chg) && isNum(b.chg) ? b.chg - a.chg : null;
  const c = el('div', 'spread');
  c.appendChild(el('span', 'spread-l', label));
  c.appendChild(el('span', 'spread-v num ' + (s < 0 ? 'down' : ''), fmtBp(s)));
  if (d != null) c.appendChild(el('span', 'spread-d chg ' + signClass(d), '1g ' + fmtBp(d)));
  c.title = s < 0 ? 'Tratto invertito' : 'Tratto positivo';
  return c;
}

function renderRates(snap) {
  const rates = arr(snap.rates);
  const cb = arr(snap.cb_events);
  if (!rates.length && !cb.length) { $('tassi').hidden = true; return; }

  const curvePts = rates
    .map((r) => ({ r: r, t: tenorYears(r.series_id) }))
    .filter((p) => p.t != null && isNum(p.r.value))
    .sort((a, b) => a.t - b.t)
    .map((p) => ({ t: p.t, label: tenorLabel(p.t), value: p.r.value, chg: p.r.chg, name: p.r.name }));
  const curveBox = $('curve');
  curveBox.textContent = '';
  let showPrev = false;
  let chart = yieldCurveChart(curvePts, curveBox.clientWidth);
  if (chart) {
    curveBox.appendChild(chart.node);
    onWidthChange(curveBox, (w) => {
      chart = yieldCurveChart(curvePts, w);
      chart.setPrev(showPrev);
      curveBox.textContent = '';
      curveBox.appendChild(chart.node);
    });
    const tools = $('curveTools');
    tools.textContent = '';
    if (chart.hasPrev) {
      tools.appendChild(segmented('Confronto', [
        { value: 'off', label: 'Oggi' }, { value: 'on', label: '+ ieri', title: 'Sovrapponi la curva della seduta precedente' }
      ], 'off', (v) => { showPrev = v === 'on'; chart.setPrev(showPrev); }).node);
    }
    const by = new Map(curvePts.map((p) => [p.label, p]));
    const spreads = $('spreads');
    spreads.textContent = '';
    [['2A–10A', '2A', '10A'], ['5A–30A', '5A', '30A'], ['3M–10A', '3M', '10A']].forEach(([l, a, b]) => {
      const c = spreadChip(l, by.get(a), by.get(b));
      if (c) spreads.appendChild(c);
    });
    const first = curvePts[0], last = curvePts[curvePts.length - 1];
    $('curveShape').textContent = last.value >= first.value ? 'Curva positiva' : 'Curva invertita';
  } else {
    $('curvePanel').hidden = true;
  }

  const tbl = $('ratesTable');
  tbl.textContent = '';
  const SHORT = { EFFR: 'Fed Funds effettivo', SOFR: 'SOFR' };
  rates.filter((r) => String(r.source || '').toUpperCase() !== 'BIS')
    .map((r) => ({ r: r, t: tenorYears(r.series_id) }))
    .sort((a, b) => (a.t == null ? 1e3 : a.t) - (b.t == null ? 1e3 : b.t))
    .forEach(({ r, t }) => {
    const row = el('div', 'rt');
    const name = el('span', 'rt-n', t != null ? 'Treasury ' + tenorLabel(t) : (SHORT[r.series_id] || r.name || r.series_id));
    name.title = r.name || r.series_id;
    row.appendChild(name);
    row.appendChild(el('span', 'rt-v num', fmtRate(r.value)));
    row.appendChild(el('span', 'chg ' + signClass(r.chg), fmtBp(r.chg)));
    row.appendChild(el('span', 'rt-d', (r.source ? r.source + ' · ' : '') + fmtDate(r.as_of)));
    tbl.appendChild(row);
    });

  const cbl = $('cbList');
  cbl.textContent = '';
  if (!cb.length) $('cbPanel').hidden = true;
  cb.forEach((ev) => {
    const row = el('div', 'rt');
    row.appendChild(el('span', 'rt-n rt-bank', ev.bank));
    row.appendChild(el('span', 'rt-v num', fmtRate(ev.rate)));
    const bp = Math.abs(isNum(ev.change_bp) ? ev.change_bp : 0);
    let chip = 'Invariato', cls = 'flat';
    if (ev.direction === 'hike') { chip = 'Rialzo +' + bp + ' bp'; cls = 'up'; }
    else if (ev.direction === 'cut') { chip = 'Taglio −' + bp + ' bp'; cls = 'down'; }
    row.appendChild(el('span', 'move ' + cls, chip));
    row.appendChild(el('span', 'rt-d', ev.as_of ? 'dal ' + fmtDate(ev.as_of) : ''));
    cbl.appendChild(row);
  });
}

/* ---------- agenda: earnings + catalysts ---------- */

/* Form 8-K item codes (EDGAR `items`, carried in trigger.topic). 9.01 is the
 * exhibits index that accompanies almost every filing, so it is only shown
 * when it is the sole item. */
const ITEMS_8K = {
  '1.01': 'Accordo rilevante', '1.02': 'Fine di un accordo rilevante', '1.03': 'Procedura concorsuale',
  '1.05': 'Incidente di cybersicurezza', '2.01': 'Acquisizione o cessione', '2.02': 'Risultati finanziari',
  '2.03': 'Nuovo debito', '2.04': 'Obbligazione accelerata', '2.05': 'Costi di ristrutturazione',
  '2.06': 'Svalutazione rilevante', '3.01': 'Requisiti di quotazione', '3.02': 'Emissione di azioni non registrata',
  '3.03': 'Modifica dei diritti degli azionisti', '4.01': 'Cambio del revisore', '4.02': 'Bilanci non più affidabili',
  '5.01': 'Cambio di controllo', '5.02': 'Cambi al vertice', '5.03': 'Modifiche a statuto',
  '5.07': 'Voto degli azionisti', '7.01': 'Comunicazione Reg FD', '8.01': 'Altri eventi', '9.01': 'Bilanci e allegati'
};
function items8k(topic) {
  const codes = String(topic || '').split(',').map((c) => c.trim()).filter(Boolean);
  const main = codes.filter((c) => c !== '9.01');
  return (main.length ? main : codes).map((c) => ITEMS_8K[c] || ('Voce ' + c));
}

function chipFilter(host, options, onChange) {
  host.textContent = '';
  let cur = options[0] && options[0].value;
  const btns = options.map((o) => {
    const b = el('button', 'fchip');
    b.type = 'button';
    b.appendChild(el('span', null, o.label));
    if (o.count != null) b.appendChild(el('span', 'fchip-n', String(o.count)));
    b.setAttribute('aria-pressed', o.value === cur ? 'true' : 'false');
    b.addEventListener('click', () => {
      cur = o.value;
      btns.forEach((x, i) => x.setAttribute('aria-pressed', options[i].value === cur ? 'true' : 'false'));
      onChange(cur);
    });
    host.appendChild(b);
    return b;
  });
}

function renderAgenda(snap) {
  const earn = arr(snap.earnings).slice().sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const trig = arr(snap.triggers);
  if (!earn.length && !trig.length) { $('agenda').hidden = true; return; }

  // earnings, filterable by day
  const eList = $('earnList');
  let eDay = null, eAll = false;
  const paintEarn = (day) => {
    if (day !== undefined) { eDay = day; eAll = false; }
    eList.textContent = '';
    const tickers = earn.map((e) => e.ticker).filter((t) => instruments.has(t));
    let lastDay = null;
    const rows = earn.filter((e) => !eDay || e.date === eDay);
    rows.slice(0, eAll ? rows.length : 8).forEach((e) => {
      if (e.date !== lastDay) {
        eList.appendChild(el('div', 'day-h', fmtDay(e.date)));
        lastDay = e.date;
      }
      const known = instruments.has(e.ticker);
      const row = el(known ? 'button' : 'div', 'erow' + (known ? ' link' : ''));
      if (known) {
        row.type = 'button';
        row.setAttribute('data-tk', e.ticker);
        row.addEventListener('click', () => openInstrument(e.ticker, tickers));
      }
      row.appendChild(el('span', 'tk', e.ticker));
      row.appendChild(el('span', 'erow-n', e.name || ''));
      row.appendChild(el('span', 'erow-s', e.sector || ''));
      row.appendChild(el('span', 'erow-e num', isNum(e.eps_estimate) ? 'EPS ' + fmtNum(e.eps_estimate, 2) : 'EPS —'));
      eList.appendChild(row);
    });
    const more = $('earnMore');
    more.hidden = rows.length <= 8;
    more.textContent = eAll ? 'Mostra meno' : 'Mostra tutte (' + rows.length + ')';
  };
  if (earn.length) {
    const days = [];
    earn.forEach((e) => { if (days.indexOf(e.date) === -1) days.push(e.date); });
    chipFilter($('earnDays'), [{ value: null, label: 'Tutte', count: earn.length }]
      .concat(days.slice(0, 8).map((d) => ({ value: d, label: fmtDay(d), count: earn.filter((e) => e.date === d).length }))),
    paintEarn);
    $('earnMore').addEventListener('click', () => { eAll = !eAll; paintEarn(); });
    paintEarn(null);
  } else {
    $('earnPanel').hidden = true;
  }

  // catalysts, filterable by kind, first 6 then "show all"
  const tList = $('trigList');
  const KIND = { sec_8k: '8-K', executive_order: 'EO' };
  let expanded = false;
  let kind = null;
  const paintTrig = () => {
    tList.textContent = '';
    const rows = trig.filter((t) => !kind || t.kind === kind);
    rows.slice(0, expanded ? rows.length : 6).forEach((t) => {
      const row = el('div', 'trow');
      const isSec = t.kind === 'sec_8k';
      row.appendChild(el('span', 'badge' + (isSec ? '' : ' eo'), KIND[t.kind] || (isSec ? '8-K' : 'EO')));
      const mid = el('div', 'trow-m');
      const labels = isSec ? items8k(t.topic) : [];
      mid.appendChild(makeSafeLink(labels.length ? t.ticker + ' · ' + labels.join(', ') : t.title, t.url, 'trow-t'));
      const meta = el('span', 'trow-d');
      meta.appendChild(document.createTextNode(fmtDate(t.date)));
      if (t.ticker) {
        meta.appendChild(document.createTextNode(' · '));
        if (instruments.has(t.ticker)) {
          const b = el('button', 'tk tk-link', t.ticker);
          b.type = 'button';
          b.addEventListener('click', () => openInstrument(t.ticker));
          meta.appendChild(b);
        } else {
          meta.appendChild(el('span', 'tk', t.ticker));
        }
      } else if (t.source === 'federal_register') {
        meta.appendChild(document.createTextNode(' · Federal Register'));
      }
      mid.appendChild(meta);
      row.appendChild(mid);
      tList.appendChild(row);
    });
    const more = $('trigMore');
    more.hidden = rows.length <= 6;
    more.textContent = expanded ? 'Mostra meno' : 'Mostra tutti (' + rows.length + ')';
  };
  if (trig.length) {
    const eo = trig.filter((t) => t.kind !== 'sec_8k').length;
    chipFilter($('trigKinds'), [
      { value: null, label: 'Tutti', count: trig.length },
      { value: 'sec_8k', label: '8-K societari', count: trig.length - eo },
      { value: 'executive_order', label: 'Ordini esecutivi', count: eo }
    ].filter((o) => o.count > 0), (v) => { kind = v; expanded = false; paintTrig(); });
    $('trigMore').addEventListener('click', () => { expanded = !expanded; paintTrig(); });
    paintTrig();
  } else {
    $('trigPanel').hidden = true;
  }
}

/* ---------- news ---------- */

function renderNews(snap) {
  const items = arr(snap.headlines).filter((h) => h && h.title);
  const list = $('newsList');
  const health = (snap.meta && snap.meta.source_health && snap.meta.source_health.gdelt) || null;
  if (health && health.status && health.status !== 'OK') {
    $('newsNote').textContent = 'Copertura parziale oggi: ' + (health.successful_queries || 0) + ' ricerche su ' +
      (health.attempted_queries || 0) + ' riuscite (limiti della fonte GDELT).';
  }
  if (!items.length) {
    list.textContent = '';
    list.appendChild(el('p', 'empty', 'Nessuna notizia economica disponibile per questa edizione.'));
    return;
  }
  const now = new Date();
  const paint = (topic) => {
    list.textContent = '';
    items.filter((h) => !topic || h.topic === topic).forEach((h) => {
      const row = el('article', 'nrow');
      const d = parseSeendate(h.seendate);
      const time = el('time', 'nrow-t', d ? relTime(d, now) : '');
      if (d) { time.setAttribute('datetime', d.toISOString()); time.title = fmtDateTime(d); }
      row.appendChild(time);
      row.appendChild(el('span', 'nrow-k', topicLabel(h.topic)));
      const mid = el('div', 'nrow-m');
      mid.appendChild(makeSafeLink(h.title, h.url, 'nrow-h'));
      mid.appendChild(el('span', 'nrow-s', outlet(h.domain)));
      row.appendChild(mid);
      list.appendChild(row);
    });
  };
  const counts = new Map();
  items.forEach((h) => counts.set(h.topic, (counts.get(h.topic) || 0) + 1));
  const topics = Array.from(counts.keys()).sort((a, b) => counts.get(b) - counts.get(a));
  chipFilter($('newsTopics'), [{ value: null, label: 'Tutte', count: items.length }]
    .concat(topics.map((t) => ({ value: t, label: topicLabel(t), count: counts.get(t) }))), paint);
  paint(null);
}

/* ---------- footer ---------- */

function renderFooter(meta) {
  const attr = (meta && meta.attribution) || {};
  const host = $('attrib');
  host.textContent = '';
  // Verbatim wording; only the files' hard line wraps are reflowed (blank
  // lines stay paragraph breaks) so the notices read as text, not a ragged column.
  ['us_treasury', 'ny_fed', 'bis', 'federal_register', 'sec_edgar'].forEach((k) => {
    if (!attr[k]) return;
    const box = el('div', 'attr');
    String(attr[k]).split(/\n\s*\n/).forEach((para) => {
      box.appendChild(el('p', null, para.replace(/\s*\n\s*/g, ' ').trim()));
    });
    host.appendChild(box);
  });
}

/* ---------- nav + scrollspy ---------- */

let activeSection = 'mercati';

function wireNav() {
  const links = [].slice.call(document.querySelectorAll('#nav a'));
  const secs = links.map((a) => $(a.getAttribute('href').slice(1))).filter((s) => s && !s.hidden);
  links.forEach((a) => {
    const s = $(a.getAttribute('href').slice(1));
    if (!s || s.hidden) a.hidden = true;
  });
  const setActive = (id) => {
    activeSection = id;
    links.forEach((a) => a.classList.toggle('on', a.getAttribute('href') === '#' + id));
  };
  const io = new IntersectionObserver((es) => {
    es.forEach((e) => { if (e.isIntersecting) setActive(e.target.id); });
  }, { rootMargin: '-40% 0px -55% 0px' });
  secs.forEach((s) => io.observe(s));
}

/* ---------- search palette entries ---------- */

function paletteEntries(snap) {
  const out = [];
  const lists = new Map(TAB_DEFS.map((t) => [t.kind, arr(snap[t.key]).map((it) => it.ticker)]));
  instruments.forEach((rec, tk) => {
    const s = rec.sector ? sectors.get(rec.sector) : null;
    out.push({
      kind: rec.kind, label: rec.item.name || tk, ticker: tk, ret: rec.item.ret_1d,
      sub: s ? s.label : '',
      run: () => openInstrument(tk, lists.get(rec.kind) && lists.get(rec.kind).indexOf(tk) !== -1 ? lists.get(rec.kind) : null)
    });
  });
  sectors.forEach((s, key) => out.push({ kind: 'Settore', label: s.label, ret: s.avg_ret_1d, run: () => openSector(key) }));
  [].slice.call(document.querySelectorAll('#nav a')).forEach((a) => {
    if (a.hidden) return;
    const id = a.getAttribute('href').slice(1);
    out.push({ kind: 'Sezione', label: a.textContent, run: () => $(id).scrollIntoView({ behavior: REDUCE.matches ? 'auto' : 'smooth' }) });
  });
  if (briefNode) out.push({ kind: 'Sezione', label: 'Morning Brief', sub: 'nota del giorno', run: openBrief });
  return out;
}

/* ---------- error state ---------- */

function showError(title, detail) {
  $('dashboard').hidden = true;
  $('tape').hidden = true;
  $('errorTitle').textContent = title;
  $('errorDetail').textContent = detail || '';
  $('errorState').hidden = false;
}

/* ---------- boot ---------- */

async function main() {
  wireChat();
  const result = await loadSnapshot();

  if (result.status === 'integrity-error') {
    showError('Integrità dei dati non verificata',
      'Lo snapshot non corrisponde al suo hash dichiarato. Rendering rifiutato per evitare dati corrotti o non aggiornati.');
    return;
  }
  if (result.status === 'schema-error') {
    showError('Versione dello schema non supportata', 'schema_version = ' + String(result.schemaVersion) + ' (attesa 1).');
    return;
  }
  if (result.status !== 'ok') {
    showError('Dati non disponibili', result.message || 'Errore di caricamento.');
    return;
  }

  const snap = result.snapshot;
  setChatSnapshot(snap);
  try {
    buildRegistry(snap);
    initDrawer({ instruments: instruments, sectors: sectors, snapshot: snap, get briefNode() { return briefNode; }, topicLabel: topicLabel });
    renderStatus(snap.meta);
    renderTape(snap);
    renderBrief(snap);
    renderMarkets(snap);
    renderMovers(snap);
    renderSectors(snap);
    renderRates(snap);
    renderAgenda(snap);
    renderNews(snap);
    renderFooter(snap.meta);
    wireNav();
    initPalette(paletteEntries(snap));
    setViewContext(() => {
      const d = drawerContext();
      if (d) return d;
      const tab = activeSection === 'mercati' && activeTab ? ', scheda "' + activeTab.label + '"' : '';
      return 'L\'utente sta guardando la sezione "' + activeSection + '"' + tab + '.';
    });
    document.body.classList.add('ready');
  } catch (e) {
    showError('Errore di rendering', e && e.message ? e.message : String(e));
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', main);
} else {
  main();
}
