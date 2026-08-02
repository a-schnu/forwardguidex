/* ForwardGuidex — dashboard renderer.
 *
 * Security posture:
 *   - Every scalar value from the snapshot is written via textContent (never innerHTML).
 *   - The ONLY innerHTML assignment is the Morning Brief, and only on DOMPurify-sanitised
 *     output of marked.parse(). External links go through setSafeExternalLink (https-only).
 *   - marked / DOMPurify are self-hosted globals loaded before this module.
 */

import { loadSnapshot } from './data.js';

/* ---------- small DOM + format helpers ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

const NF2 = new Intl.NumberFormat('it-IT', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function fmtNum(x) {
  return typeof x === 'number' && Number.isFinite(x) ? NF2.format(x) : '—';
}
function fmtPct(x) {
  if (typeof x !== 'number' || !Number.isFinite(x)) return '—';
  return (x > 0 ? '+' : '') + NF2.format(x) + '%';
}
function signClass(x) {
  if (typeof x !== 'number' || !Number.isFinite(x) || x === 0) return 'flat';
  return x > 0 ? 'up' : 'down';
}
function fmtDateTime(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso || '—');
  return new Intl.DateTimeFormat('it-IT', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }).format(d) + ' UTC';
}
function fmtDate(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso || '—');
  return new Intl.DateTimeFormat('it-IT', { dateStyle: 'medium', timeZone: 'UTC' }).format(d);
}
function fmtSeendate(s) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(String(s || ''));
  if (!m) return String(s || '');
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
  return new Intl.DateTimeFormat('it-IT', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }).format(d) + ' UTC';
}

/**
 * Safe external link. Only https: URLs become anchors (with rel/target hardening);
 * anything else is rendered as inert plain text.
 */
function makeSafeLink(text, url) {
  let ok = false;
  try { ok = new URL(url, location.href).protocol === 'https:'; } catch (_e) { ok = false; }
  if (ok) {
    const a = el('a', 'ext-link', text);
    a.setAttribute('href', url);
    a.setAttribute('rel', 'noopener noreferrer');
    a.setAttribute('target', '_blank');
    return a;
  }
  return el('span', 'ext-link disabled', text);
}

/**
 * Harden an existing anchor (from sanitised brief HTML). Replaces non-https links
 * with inert text; hardens https links with rel/target.
 */
function setSafeExternalLink(a) {
  const url = a.getAttribute('href') || '';
  let ok = false;
  try { ok = new URL(url, location.href).protocol === 'https:'; } catch (_e) { ok = false; }
  if (ok) {
    a.setAttribute('rel', 'noopener noreferrer');
    a.setAttribute('target', '_blank');
    return;
  }
  const span = el('span', 'ext-link disabled', a.textContent || url);
  if (a.parentNode) a.parentNode.replaceChild(span, a);
}

/* ---------- inline SVG + motion helpers (CSP-safe: no external libs) ----------
 * Charts are built as inline SVG via createElementNS; geometry is set via
 * ATTRIBUTES (setAttribute), never a style="" string. Animation is driven by CSS
 * classes/keyframes in base.css; the only CSSOM writes are el.style.setProperty
 * (custom props like --spark-len) and tooltip positioning, both permitted by CSP.
 */

const SVGNS = 'http://www.w3.org/2000/svg';
const REDUCE = window.matchMedia('(prefers-reduced-motion: reduce)');

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVGNS, tag);
  if (attrs) {
    for (const k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) node.setAttribute(k, attrs[k]);
    }
  }
  return node;
}

/** Polyline length from an array of [x,y] points (no DOM needed). */
function polyLen(points) {
  let len = 0;
  for (let i = 1; i < points.length; i++) {
    len += Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
  }
  return len;
}

/**
 * Small sparkline from item.spark (array of numbers, may be undefined/sparse).
 * Returns an <svg> element, or null when there isn't enough data to draw.
 */
function buildSparkline(item) {
  const raw = Array.isArray(item && item.spark)
    ? item.spark.filter((v) => typeof v === 'number' && Number.isFinite(v))
    : [];
  if (raw.length < 2) return null;

  const W = 120, H = 32, pad = 3;
  const min = Math.min.apply(null, raw);
  const max = Math.max.apply(null, raw);
  const span = (max - min) || 1;
  const n = raw.length;
  const xAt = (i) => pad + (i / (n - 1)) * (W - 2 * pad);
  const yAt = (v) => pad + (1 - (v - min) / span) * (H - 2 * pad);

  const pts = raw.map((v, i) => [xAt(i), yAt(v)]);
  const line = 'M' + pts.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' L');
  const area = line
    + ' L' + xAt(n - 1).toFixed(1) + ',' + (H - pad).toFixed(1)
    + ' L' + xAt(0).toFixed(1) + ',' + (H - pad).toFixed(1) + ' Z';
  const len = Math.ceil(polyLen(pts));

  // Colour follows the line's OWN trend (last vs first finite value), not ret_1d.
  const trend = signClass(raw[raw.length - 1] - raw[0]);

  const svg = svgEl('svg', {
    'class': 'spark ' + trend,
    viewBox: '0 0 ' + W + ' ' + H,
    preserveAspectRatio: 'none',
    'aria-hidden': 'true'
  });
  svg.appendChild(svgEl('path', { 'class': 'spark-area', d: area }));
  const lineEl = svgEl('path', { 'class': 'spark-line', d: line, 'stroke-dasharray': len });
  lineEl.style.setProperty('--spark-len', String(len));
  svg.appendChild(lineEl);
  return svg;
}

/**
 * Diverging horizontal bar (SVG rect) around a centre baseline. Used by the
 * sector overview chart and the sector-detail constituents chart. Growth is a
 * CSS keyframe; positive bars grow from the left of centre, negative from the right.
 */
function divergingBar(value, maxAbs, opts) {
  const W = 200, H = (opts && opts.h) || 18, cx = W / 2;
  const frac = maxAbs > 0 ? Math.min(1, Math.abs(value) / maxAbs) : 0;
  const w = Math.max(frac * (W / 2 - 2), 0.6);
  const pos = value >= 0;
  const svg = svgEl('svg', {
    'class': (opts && opts.cls) || 'div-svg',
    viewBox: '0 0 ' + W + ' ' + H,
    preserveAspectRatio: 'none',
    'aria-hidden': 'true'
  });
  svg.appendChild(svgEl('line', { 'class': 'bar-zero', x1: cx, y1: 0, x2: cx, y2: H }));
  const barClass = 'bar ' + (pos ? 'pos' : 'neg') + (opts && opts.color ? ' ' + opts.color : '');
  svg.appendChild(svgEl('rect', {
    'class': barClass,
    x: (pos ? cx : cx - w).toFixed(1), y: Math.round(H * 0.14),
    width: w.toFixed(1), height: Math.round(H * 0.72), rx: 2
  }));
  return svg;
}

/* ---------- count-up + chart-draw on scroll into view ---------- */

function fmtCountUp(v, kind) {
  if (kind === 'pctval') return fmtNum(v) + '%';
  return fmtNum(v);
}

function animateValue(node) {
  const target = parseFloat(node.getAttribute('data-countup'));
  const finalText = node.getAttribute('data-final');
  if (!Number.isFinite(target)) { if (finalText != null) node.textContent = finalText; return; }
  const kind = node.getAttribute('data-fmt') || '';
  const dur = 850, t0 = performance.now();
  function frame(now) {
    const p = Math.min(1, (now - t0) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    node.textContent = fmtCountUp(target * eased, kind);
    if (p < 1) requestAnimationFrame(frame);
    else if (finalText != null) node.textContent = finalText;
  }
  requestAnimationFrame(frame);
}

function runCountUp(root) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll('[data-countup]').forEach(animateValue);
}

/** Marks a value node for count-up (falls back to its own text if never triggered). */
function markCountUp(node, value, kind) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    node.setAttribute('data-countup', value);
    node.setAttribute('data-final', node.textContent);
    if (kind) node.setAttribute('data-fmt', kind);
  }
  return node;
}

let vizObserver = null;

/** Observe [data-viz] hosts: add `.viz` (drives CSS chart keyframes) + count-up. */
function wireViz() {
  const hosts = document.querySelectorAll('[data-viz]');
  if (REDUCE.matches) {
    // No motion: leave charts in their static (fully drawn) state, values final.
    return;
  }
  vizObserver = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (en.isIntersecting) {
        en.target.classList.add('viz');
        runCountUp(en.target);
        vizObserver.unobserve(en.target);
      }
    });
  }, { threshold: 0.2 });
  hosts.forEach((h) => vizObserver.observe(h));
}

/* ---------- top bar ---------- */

function renderTopBar(meta) {
  document.getElementById('dataAsOf').textContent = fmtDateTime(meta.data_as_of);

  if (meta && meta.is_demo) {
    const ribbon = document.getElementById('demoRibbon');
    if (ribbon) ribbon.hidden = false;
  }
}

/* ---------- KPI strip ---------- */

/**
 * Cosmetic-only ticker -> region map for grouping the overview. No schema field
 * for region; anything unmapped falls into "Altri" (defensive).
 */
const REGION_BY_TICKER = {
  // Americhe
  '^GSPC': 'Americhe', '^NDX': 'Americhe', '^DJI': 'Americhe', '^RUT': 'Americhe',
  // Europa
  '^STOXX': 'Europa', '^STOXX50E': 'Europa', '^GDAXI': 'Europa', '^FCHI': 'Europa',
  '^FTSE': 'Europa', 'FTSEMIB.MI': 'Europa',
  // Asia
  '^N225': 'Asia', '^HSI': 'Asia', '000001.SS': 'Asia', '^KS11': 'Asia'
};
const REGION_ORDER = ['Americhe', 'Europa', 'Asia', 'Altri'];

/** Short factual description shown under the active Overview tab (§9c). */
const TAB_DESC = {
  indici: 'Principali listini azionari di Americhe, Europa e Asia.',
  futures: 'Energia, metalli, prodotti agricoli e cambi.',
  etf: 'Fondi quotati su indici, aree geografiche e temi.',
  crypto: 'Le principali criptovalute per capitalizzazione.'
};

function kpiCard(item) {
  const card = el('div', 'kpi glass');
  card.setAttribute('data-viz', '');

  const inner = el('div', 'kpi-inner');

  // Front face: name, price, returns, sparkline.
  const front = el('div', 'kpi-front');
  front.appendChild(el('div', 'kpi-name', item.name));
  const val = el('div', 'kpi-val');
  val.appendChild(markCountUp(el('span', 'kpi-last', fmtNum(item.last)), item.last));
  val.appendChild(el('span', 'kpi-ccy', item.currency || ''));
  front.appendChild(val);
  const rets = el('div', 'kpi-rets');
  rets.appendChild(el('span', 'ret ' + signClass(item.ret_1d), fmtPct(item.ret_1d)));
  rets.appendChild(el('span', 'ret-5 muted', '5gg ' + fmtPct(item.ret_5d)));
  front.appendChild(rets);
  const spark = buildSparkline(item);
  if (spark) {
    const holder = el('div', 'kpi-spark');
    holder.appendChild(spark);
    front.appendChild(holder);
  }
  inner.appendChild(front);

  // Flip-to-EUR only makes sense for NON-EUR instruments with a distinct EUR price.
  const eurNum = (typeof item.eur === 'number' && Number.isFinite(item.eur)) ? item.eur : null;
  const flippable = item.currency !== 'EUR' && eurNum != null && eurNum !== item.last;

  if (flippable) {
    card.classList.add('flippable');
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.setAttribute('aria-pressed', 'false');
    card.setAttribute('aria-label', (item.name || item.ticker || 'Strumento') + ' — tocca per il prezzo in EUR');

    const back = el('div', 'kpi-back');
    const backVal = el('div', 'kpi-back-val');
    backVal.appendChild(el('span', 'kpi-last', fmtNum(eurNum)));
    backVal.appendChild(el('span', 'kpi-ccy', 'EUR'));
    back.appendChild(backVal);
    inner.appendChild(back);

    const flip = () => {
      const flipped = card.classList.toggle('flipped');
      card.setAttribute('aria-pressed', flipped ? 'true' : 'false');
    };
    card.addEventListener('click', flip);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); flip(); }
    });
  }

  card.appendChild(inner);
  return card;
}

function kpiGroup(label, items) {
  const group = el('div', 'kpi-group');
  if (label) group.appendChild(el('div', 'kpi-group-label', label));
  const grid = el('div', 'kpi-grid');
  items.forEach((it) => grid.appendChild(kpiCard(it)));
  group.appendChild(grid);
  return group;
}

/** A flat grid of KPI cards (futures / ETF / crypto tab panels). */
function buildKpiGrid(items) {
  const grid = el('div', 'kpi-grid');
  items.forEach((it) => grid.appendChild(kpiCard(it)));
  return grid;
}

/** Indices tab panel: KPI cards grouped by region (Americhe / Europa / Asia). */
function buildIndicesPanel(indices) {
  const wrap = el('div', 'kpi-groups');
  const buckets = new Map(REGION_ORDER.map((r) => [r, []]));
  indices.forEach((it) => {
    const region = REGION_BY_TICKER[it.ticker] || 'Altri';
    buckets.get(region).push(it);
  });
  REGION_ORDER.forEach((region) => {
    const items = buckets.get(region);
    if (items && items.length) wrap.appendChild(kpiGroup(region, items));
  });
  return wrap;
}

/**
 * Overview as accessible tabs by asset class. Tabs with no data are omitted; the
 * first available tab is selected by default. Full ARIA tab pattern + arrow nav.
 */
function renderOverview(snapshot) {
  const tablist = document.getElementById('ovTablist');
  const panels = document.getElementById('ovPanels');
  const desc = document.getElementById('ovTabDesc');
  if (!tablist || !panels) return;
  tablist.textContent = '';
  panels.textContent = '';

  const indices = Array.isArray(snapshot.indices) ? snapshot.indices : [];
  const futures = Array.isArray(snapshot.futures) ? snapshot.futures : [];
  const etfs = Array.isArray(snapshot.etfs) ? snapshot.etfs : [];
  const crypto = Array.isArray(snapshot.crypto) ? snapshot.crypto : [];

  const defs = [
    { id: 'indici', label: 'Indici', has: indices.length, build: () => buildIndicesPanel(indices) },
    { id: 'futures', label: 'Futures & materie prime', has: futures.length, build: () => buildKpiGrid(futures) },
    { id: 'etf', label: 'ETF', has: etfs.length, build: () => buildKpiGrid(etfs) },
    { id: 'crypto', label: 'Crypto', has: crypto.length, build: () => buildKpiGrid(crypto) }
  ].filter((t) => t.has);
  if (!defs.length) return;
  if (desc) desc.textContent = TAB_DESC[defs[0].id] || '';

  const btns = [], pans = [];
  defs.forEach((t, i) => {
    const btn = el('button', 'ov-tab', t.label);
    btn.id = 'ovtab-' + t.id;
    btn.type = 'button';
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-controls', 'ovpanel-' + t.id);
    btn.setAttribute('aria-selected', i === 0 ? 'true' : 'false');
    btn.setAttribute('tabindex', i === 0 ? '0' : '-1');
    tablist.appendChild(btn);

    const panel = el('div', 'ov-panel');
    panel.id = 'ovpanel-' + t.id;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', 'ovtab-' + t.id);
    panel.setAttribute('tabindex', '0');
    panel.hidden = i !== 0;
    panel.appendChild(t.build());
    panels.appendChild(panel);

    btns.push(btn); pans.push(panel);
  });

  const select = (idx, focus) => {
    btns.forEach((b, k) => {
      const on = k === idx;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.setAttribute('tabindex', on ? '0' : '-1');
      pans[k].hidden = !on;
    });
    if (desc) desc.textContent = TAB_DESC[defs[idx].id] || '';
    if (focus) btns[idx].focus();
  };

  btns.forEach((b, i) => {
    b.addEventListener('click', () => select(i, false));
    b.addEventListener('keydown', (e) => {
      let ni = null;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') ni = (i + 1) % btns.length;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') ni = (i - 1 + btns.length) % btns.length;
      else if (e.key === 'Home') ni = 0;
      else if (e.key === 'End') ni = btns.length - 1;
      if (ni != null) { e.preventDefault(); select(ni, true); }
    });
  });
}

/* ---------- sectors (data-driven cards + zoom modal) ---------- */

const sectorsByKey = new Map();

function sectorCard(sector) {
  const card = el('div', 'scard glass');
  card.setAttribute('data-key', sector.key);
  card.setAttribute('role', 'button');
  card.setAttribute('tabindex', '0');
  card.setAttribute('aria-label', sector.label + ' — apri il dettaglio');

  const top = el('div');
  top.appendChild(el('div', 'tag', 'Settore'));
  top.appendChild(el('h3', null, sector.label));
  const tickers = []
    .concat(sector.etfs || [], sector.constituents || [])
    .map((x) => x.ticker)
    .filter(Boolean)
    .join(' · ');
  top.appendChild(el('div', 'desc', tickers));
  card.appendChild(top);

  const foot = el('div', 'foot');
  const left = el('div');
  left.appendChild(el('div', 'pct ' + signClass(sector.avg_ret_1d), fmtPct(sector.avg_ret_1d)));
  left.appendChild(el('div', 'etfs', '5gg ' + fmtPct(sector.avg_ret_5d)));
  foot.appendChild(left);
  foot.appendChild(el('span', 'more', 'Apri ↗'));
  card.appendChild(foot);
  return card;
}

function itemsTable(title, items) {
  const panel = el('div', 'panel');
  panel.appendChild(el('h5', null, title));
  const table = el('table', 'tbl');
  const thead = el('thead');
  const htr = el('tr');
  [['Ticker', ''], ['Nome', ''], ['Ultimo', 'r'], ['1g', 'r'], ['5gg', 'r']].forEach((h) => {
    htr.appendChild(el('th', h[1] || null, h[0]));
  });
  thead.appendChild(htr);
  table.appendChild(thead);
  const tbody = el('tbody');
  (items || []).forEach((it) => {
    const tr = el('tr');
    tr.appendChild(el('td', 'tk', it.ticker));
    tr.appendChild(el('td', 'nm', it.name));
    tr.appendChild(el('td', 'num', fmtNum(it.last)));
    tr.appendChild(el('td', 'num ' + signClass(it.ret_1d), fmtPct(it.ret_1d)));
    tr.appendChild(el('td', 'num ' + signClass(it.ret_5d), fmtPct(it.ret_5d)));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  panel.appendChild(table);
  return panel;
}

/** Diverging bar chart of every sector's avg 1d return, sorted, above the carousel. */
function renderSectorBars(sectors) {
  const host = document.getElementById('sectorBars');
  if (!host) return;
  host.textContent = '';
  const list = (Array.isArray(sectors) ? sectors : [])
    .filter((s) => Number.isFinite(s.avg_ret_1d))
    .slice()
    .sort((a, b) => b.avg_ret_1d - a.avg_ret_1d);
  if (list.length < 2) { host.hidden = true; return; }
  host.hidden = false;
  const maxAbs = Math.max.apply(null, list.map((s) => Math.abs(s.avg_ret_1d)).concat(0.01));
  list.forEach((s) => {
    const row = el('div', 'sbar-row');
    row.appendChild(el('div', 'sbar-label', s.label));
    const track = el('div', 'sbar-track');
    track.appendChild(divergingBar(s.avg_ret_1d, maxAbs, { cls: 'sbar-svg', color: signClass(s.avg_ret_1d) }));
    row.appendChild(track);
    row.appendChild(el('div', 'sbar-val ' + signClass(s.avg_ret_1d), fmtPct(s.avg_ret_1d)));
    host.appendChild(row);
  });
  host.setAttribute('data-viz', '');
}

/** Small yellow-accented bar chart of constituents' 1d returns (sector modal). */
function constituentsChart(items) {
  const list = (Array.isArray(items) ? items : []).filter((it) => Number.isFinite(it.ret_1d));
  if (list.length < 2) return null;
  const panel = el('div', 'panel cpanel');
  panel.appendChild(el('h5', null, 'Rendimenti 1g'));
  const chart = el('div', 'cbars');
  const maxAbs = Math.max.apply(null, list.map((it) => Math.abs(it.ret_1d)).concat(0.01));
  list.forEach((it) => {
    const row = el('div', 'cbar-row');
    row.appendChild(el('div', 'cbar-label', it.name || it.ticker));
    const track = el('div', 'cbar-track');
    track.appendChild(divergingBar(it.ret_1d, maxAbs, { cls: 'cbar-svg', h: 16, color: 'cbar' }));
    row.appendChild(track);
    row.appendChild(el('div', 'cbar-val ' + signClass(it.ret_1d), fmtPct(it.ret_1d)));
    chart.appendChild(row);
  });
  panel.appendChild(chart);
  return panel;
}

let lastFocusedBeforeModal = null;

function openSectorModal(card) {
  const sector = sectorsByKey.get(card.getAttribute('data-key'));
  if (!sector) return;
  const body = document.getElementById('sheetBody');
  body.textContent = '';

  body.appendChild(el('div', 's-tag', 'Settore'));
  const title = el('div', 's-title');
  title.appendChild(el('h3', null, sector.label));
  title.appendChild(el('span', 's-pct ' + signClass(sector.avg_ret_1d), fmtPct(sector.avg_ret_1d)));
  body.appendChild(title);

  // Yellow-accented key figures (labels stay muted, values pop).
  const why = el('p', 's-why');
  why.appendChild(document.createTextNode('Media 1 giorno '));
  why.appendChild(el('span', 'accent-y', fmtPct(sector.avg_ret_1d)));
  why.appendChild(document.createTextNode(' · Media 5 giorni '));
  why.appendChild(el('span', 'accent-y', fmtPct(sector.avg_ret_5d)));
  body.appendChild(why);

  const cchart = constituentsChart(sector.constituents);
  if (cchart) body.appendChild(cchart);

  const grid = el('div', 's-grid');
  grid.appendChild(itemsTable('ETF', sector.etfs));
  grid.appendChild(itemsTable('Titoli', sector.constituents));
  body.appendChild(grid);

  // Trigger the bar-grow keyframe (skipped under reduced motion — bars stay drawn).
  body.classList.remove('viz');
  if (!REDUCE.matches) { void body.offsetWidth; body.classList.add('viz'); }

  const overlay = document.getElementById('overlay');
  const wrap = document.getElementById('sheetWrap');
  const r = card.getBoundingClientRect();
  wrap.style.setProperty('--ox', (r.left + r.width / 2) + 'px');
  wrap.style.setProperty('--oy', (r.top + r.height / 2) + 'px');
  overlay.style.display = 'block';
  void overlay.offsetWidth; // force reflow so the zoom transition runs
  overlay.classList.add('open');
  overlay.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';

  // Move focus into the dialog for keyboard/AT users; remember where to return.
  lastFocusedBeforeModal = card;
  const closeBtn = document.getElementById('close');
  if (closeBtn) closeBtn.focus();
}

function closeModal() {
  const overlay = document.getElementById('overlay');
  overlay.classList.remove('open');
  overlay.setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
  if (lastFocusedBeforeModal && typeof lastFocusedBeforeModal.focus === 'function') {
    lastFocusedBeforeModal.focus();
    lastFocusedBeforeModal = null;
  }
  setTimeout(() => {
    if (!overlay.classList.contains('open')) overlay.style.display = 'none';
  }, 560);
}

function renderSectors(snapshot) {
  const track = document.getElementById('track');
  const dots = document.getElementById('dots');
  track.textContent = '';
  dots.textContent = '';
  sectorsByKey.clear();

  const sectors = Array.isArray(snapshot.sectors) ? snapshot.sectors : [];
  renderSectorBars(sectors);
  sectors.forEach((s) => {
    sectorsByKey.set(s.key, s);
    track.appendChild(sectorCard(s));
  });

  const cards = [].slice.call(track.children);

  // dots
  cards.forEach((_, i) => {
    const b = el('button', 'dot' + (i === 0 ? ' active' : ''));
    b.setAttribute('aria-label', 'Vai al settore ' + (i + 1));
    b.addEventListener('click', () => {
      track.scrollTo({ left: cards[i].offsetLeft - cards[0].offsetLeft, behavior: 'smooth' });
    });
    dots.appendChild(b);
  });
  const dotEls = [].slice.call(dots.children);
  const step = () => (cards.length > 1 ? cards[1].offsetLeft - cards[0].offsetLeft : 1);
  track.addEventListener('scroll', () => {
    const i = Math.max(0, Math.min(cards.length - 1, Math.round(track.scrollLeft / step())));
    dotEls.forEach((d, k) => d.classList.toggle('active', k === i));
  });

  // drag-to-scroll + click-to-open
  let down = false, moved = false, sx = 0, sl = 0;
  track.addEventListener('pointerdown', (e) => { down = true; moved = false; sx = e.pageX; sl = track.scrollLeft; });
  window.addEventListener('pointermove', (e) => {
    if (!down) return;
    if (Math.abs(e.pageX - sx) > 6) { moved = true; track.classList.add('drag'); }
    track.scrollLeft = sl - (e.pageX - sx);
  });
  window.addEventListener('pointerup', () => { down = false; track.classList.remove('drag'); });
  cards.forEach((c) => {
    c.addEventListener('click', () => { if (!moved) openSectorModal(c); });
    c.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault();
        openSectorModal(c);
      }
    });
  });
}

/* ---------- rates ---------- */

function rateChip(r) {
  const chip = el('div', 'rate glass');
  chip.setAttribute('data-viz', '');
  chip.appendChild(el('div', 'rate-name', r.name));
  const row = el('div', 'rate-row');
  row.appendChild(markCountUp(el('span', 'rate-val', fmtNum(r.value) + '%'), r.value, 'pctval'));
  row.appendChild(el('span', 'rate-chg ' + signClass(r.chg), fmtPct(r.chg)));
  chip.appendChild(row);
  const meta = el('div', 'rate-meta');
  meta.appendChild(el('span', 'src', r.source));
  meta.appendChild(el('span', 'asof', fmtDate(r.as_of)));
  chip.appendChild(meta);
  return chip;
}

/* ---------- yield curve chart (inline SVG line/area + hover tooltip) ---------- */

/** Parse a Treasury tenor (years) from a series_id like UST2Y / UST3M / UST10Y. */
function tenorYears(seriesId) {
  const m = /UST\s*(\d+(?:\.\d+)?)\s*(M|Y)/i.exec(seriesId || '');
  if (!m) return null;
  const n = parseFloat(m[1]);
  return m[2].toUpperCase() === 'M' ? n / 12 : n;
}

function tenorLabel(years) {
  if (years < 1) return Math.round(years * 12) + 'M';
  return (Number.isInteger(years) ? years : years.toFixed(1)) + 'A';
}

function renderYieldCurve(rates) {
  const wrap = document.getElementById('yieldCurveWrap');
  const host = document.getElementById('yieldCurve');
  const tip = document.getElementById('ycTip');
  if (!wrap || !host) return;
  host.textContent = '';

  const pts = (Array.isArray(rates) ? rates : [])
    .map((r) => ({ r: r, t: tenorYears(r.series_id) }))
    .filter((p) => p.t != null && Number.isFinite(p.r.value))
    .sort((a, b) => a.t - b.t);

  if (pts.length < 2) { wrap.hidden = true; return; }
  wrap.hidden = false;

  const W = 640, H = 250;
  const m = { l: 46, r: 18, t: 20, b: 40 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;

  const tMin = pts[0].t, tMax = pts[pts.length - 1].t;
  const tSpan = (tMax - tMin) || 1;
  const vals = pts.map((p) => p.r.value);
  let vMin = Math.min.apply(null, vals), vMax = Math.max.apply(null, vals);
  const vPad = ((vMax - vMin) || 1) * 0.25;
  vMin -= vPad; vMax += vPad;
  const vSpan = (vMax - vMin) || 1;

  const xAt = (t) => m.l + ((t - tMin) / tSpan) * pw;
  const yAt = (v) => m.t + (1 - (v - vMin) / vSpan) * ph;

  const svg = svgEl('svg', {
    'class': 'yc-svg', viewBox: '0 0 ' + W + ' ' + H,
    role: 'img', 'aria-label': 'Curva dei rendimenti dei Treasury USA'
  });

  // y gridlines + labels (4 ticks)
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = vMin + (vSpan * i) / ticks;
    const y = yAt(v);
    svg.appendChild(svgEl('line', { 'class': 'yc-grid', x1: m.l, y1: y.toFixed(1), x2: W - m.r, y2: y.toFixed(1) }));
    const lbl = svgEl('text', { 'class': 'yc-tick-label', x: m.l - 8, y: (y + 4).toFixed(1), 'text-anchor': 'end' });
    lbl.textContent = fmtNum(v) + '%';
    svg.appendChild(lbl);
  }

  // area + line
  const linePts = pts.map((p) => [xAt(p.t), yAt(p.r.value)]);
  const lineD = 'M' + linePts.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' L');
  const areaD = lineD
    + ' L' + linePts[linePts.length - 1][0].toFixed(1) + ',' + (m.t + ph).toFixed(1)
    + ' L' + linePts[0][0].toFixed(1) + ',' + (m.t + ph).toFixed(1) + ' Z';
  svg.appendChild(svgEl('path', { 'class': 'yc-area', d: areaD }));
  const lineEl = svgEl('path', { 'class': 'yc-line', d: lineD, 'stroke-dasharray': Math.ceil(polyLen(linePts)) });
  lineEl.style.setProperty('--spark-len', String(Math.ceil(polyLen(linePts))));
  svg.appendChild(lineEl);

  // x labels + interactive points
  pts.forEach((p, i) => {
    const x = xAt(p.t), y = yAt(p.r.value);
    const xl = svgEl('text', { 'class': 'yc-tick-label', x: x.toFixed(1), y: (H - m.b + 20).toFixed(1), 'text-anchor': 'middle' });
    xl.textContent = tenorLabel(p.t);
    svg.appendChild(xl);

    const dot = svgEl('circle', {
      'class': 'yc-dot', cx: x.toFixed(1), cy: y.toFixed(1), r: 4,
      tabindex: '0', role: 'img',
      'aria-label': p.r.name + ': ' + fmtNum(p.r.value) + '%'
    });
    const showTip = () => {
      if (!tip) return;
      tip.textContent = '';
      const b = el('b', null, tenorLabel(p.t) + ' · ');
      tip.appendChild(b);
      tip.appendChild(document.createTextNode(fmtNum(p.r.value) + '%'));
      tip.style.setProperty('left', ((x / W) * 100) + '%');
      tip.style.setProperty('top', ((y / H) * 100) + '%');
      tip.classList.add('show');
    };
    const hideTip = () => { if (tip) tip.classList.remove('show'); };
    dot.addEventListener('mouseenter', showTip);
    dot.addEventListener('mouseleave', hideTip);
    dot.addEventListener('focus', showTip);
    dot.addEventListener('blur', hideTip);
    svg.appendChild(dot);
  });

  host.appendChild(svg);
}

function ratesGroup(label, rates) {
  const group = el('div', 'rates-group');
  if (label) group.appendChild(el('div', 'kpi-group-label', label));
  const strip = el('div', 'rates-strip');
  rates.forEach((r) => strip.appendChild(rateChip(r)));
  group.appendChild(strip);
  return group;
}

function renderRates(snapshot) {
  const host = document.getElementById('ratesStrip');
  host.textContent = '';
  const rates = Array.isArray(snapshot.rates) ? snapshot.rates : [];
  renderYieldCurve(rates);
  // Central-bank policy rates arrive via the BIS source; keep them visually
  // separated from Treasury/reference rates when both are present.
  const isCentralBank = (r) => String(r.source || '').toUpperCase() === 'BIS';
  const cb = rates.filter(isCentralBank);
  const other = rates.filter((r) => !isCentralBank(r));

  if (cb.length && other.length) {
    host.classList.add('rates-grouped');
    host.appendChild(ratesGroup('Treasury & riferimento', other));
    host.appendChild(ratesGroup('Banche centrali', cb));
  } else {
    host.classList.remove('rates-grouped');
    rates.forEach((r) => host.appendChild(rateChip(r)));
  }
}

/* ---------- movers ---------- */

function moverRow(item, maxAbs) {
  const row = el('div', 'mv-row');
  row.appendChild(el('span', 'mv-tk', item.ticker));
  row.appendChild(el('span', 'mv-nm', item.name));
  const s = el('span', 'mv-sec muted', item.sector || '');
  row.appendChild(s);
  row.appendChild(el('span', 'mv-ret ' + signClass(item.ret_1d), fmtPct(item.ret_1d)));

  // Magnitude mini-bar spanning the row (width proportional to |ret_1d|).
  if (Number.isFinite(item.ret_1d)) {
    const W = 100, H = 6;
    const frac = maxAbs > 0 ? Math.min(1, Math.abs(item.ret_1d) / maxAbs) : 0;
    const w = Math.max(frac * W, 0.6);
    const svg = svgEl('svg', {
      'class': 'mv-bar-svg', viewBox: '0 0 ' + W + ' ' + H,
      preserveAspectRatio: 'none', 'aria-hidden': 'true'
    });
    svg.appendChild(svgEl('rect', {
      'class': 'bar pos ' + signClass(item.ret_1d),
      x: 0, y: 1, width: w.toFixed(1), height: H - 2, rx: 2
    }));
    const barWrap = el('div', 'mv-bar');
    barWrap.appendChild(svg);
    row.appendChild(barWrap);
  }
  return row;
}

function renderMovers(snapshot) {
  const movers = snapshot.movers || {};
  const g = document.getElementById('moversGainers');
  const l = document.getElementById('moversLosers');
  g.textContent = '';
  l.textContent = '';
  const gainers = movers.gainers || [];
  const losers = movers.losers || [];
  const maxAbs = Math.max.apply(null,
    gainers.concat(losers).map((it) => Math.abs(it.ret_1d) || 0).concat(0.01));
  gainers.forEach((it) => g.appendChild(moverRow(it, maxAbs)));
  losers.forEach((it) => l.appendChild(moverRow(it, maxAbs)));
  g.setAttribute('data-viz', '');
  l.setAttribute('data-viz', '');
}

/* ---------- headlines ---------- */

function renderHeadlines(snapshot) {
  const host = document.getElementById('headlines');
  host.textContent = '';
  (Array.isArray(snapshot.headlines) ? snapshot.headlines : []).forEach((h) => {
    const item = el('div', 'hl glass');
    const head = el('div', 'hl-head');
    head.appendChild(el('span', 'hl-topic', h.topic));
    head.appendChild(el('span', 'hl-when muted', fmtSeendate(h.seendate)));
    item.appendChild(head);
    const titleWrap = el('div', 'hl-title');
    titleWrap.appendChild(makeSafeLink(h.title, h.url));
    item.appendChild(titleWrap);
    item.appendChild(el('div', 'hl-domain muted', h.domain));
    host.appendChild(item);
  });
}

/* ---------- brief (sanitised markdown) ---------- */

function renderBrief(snapshot) {
  const brief = snapshot.brief || {};
  document.getElementById('briefWhen').textContent =
    'Sintesi generata con AI' + (brief.created_at ? ' · ' + fmtDateTime(brief.created_at) : '');

  const container = document.getElementById('briefBody');
  const md = typeof brief.markdown === 'string' ? brief.markdown : '';
  const rawHtml = window.marked.parse(md);
  const clean = window.DOMPurify.sanitize(rawHtml, {
    ALLOWED_TAGS: ['h1', 'h2', 'h3', 'p', 'ul', 'ol', 'li', 'strong', 'em', 'blockquote', 'code', 'pre', 'a', 'br'],
    ALLOWED_ATTR: ['href', 'title'],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false
  });
  // Only place innerHTML is used, and only on sanitised output.
  container.innerHTML = clean;
  // Harden any links the brief produced.
  container.querySelectorAll('a').forEach(setSafeExternalLink);
  // Cosmetic post-processing of the already-sanitised DOM (classes + icons only).
  enhanceBrief(container);
}

/**
 * Enrich the sanitised brief DOM. STRICTLY class additions and prepended
 * textContent icon spans — no new innerHTML, so the security posture is intact.
 */
function enhanceBrief(container) {
  try {
    // 1. Leading regime paragraph -> coloured pill banner. The regime line is a
    //    bold sentence: detect it by keyword OR by being (almost) fully bold.
    const first = container.firstElementChild;
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

    // 2. Headings -> class + a leading emoji icon chosen by heading text (IT + EN).
    container.querySelectorAll('h1, h2, h3').forEach((h) => {
      h.classList.add('brief-h');
      const t = (h.textContent || '').toLowerCase();
      let icon = '•';
      if (t.indexOf('sintesi') !== -1 || t.indexOf('tl;dr') !== -1 || t.indexOf('tldr') !== -1) icon = '📌';
      else if (t.indexOf('occhio') !== -1 || t.indexOf('watch') !== -1 || t.indexOf('osserv') !== -1) icon = '👁';
      else if (t.indexOf('settor') !== -1 || t.indexOf('sector') !== -1) icon = '🏭';
      else if (t.indexOf('tass') !== -1 || t.indexOf('rate') !== -1 || t.indexOf('central') !== -1) icon = '📈';
      h.insertBefore(el('span', 'brief-ic', icon), h.firstChild);
    });

    // 3. "Breve termine" / "Lungo termine" strong labels -> two accent blocks
    //    (+ their following list). English Short-/Long-term kept as a fallback.
    container.querySelectorAll('strong').forEach((s) => {
      const t = (s.textContent || '').toLowerCase().trim();
      let kind = null;
      if (/^breve\s*termine|^short[- ]?term/.test(t)) kind = 'short';
      else if (/^lungo\s*termine|^long[- ]?term/.test(t)) kind = 'long';
      if (!kind) return;
      const block = (s.closest && s.closest('p, li')) || s.parentElement;
      if (!block) return;
      block.classList.add('brief-' + kind);
      const next = block.nextElementSibling;
      if (next && (next.tagName === 'UL' || next.tagName === 'OL')) {
        next.classList.add('brief-' + kind, 'brief-' + kind + '-list');
      }
    });
  } catch (_e) { /* enhancement is cosmetic; never block the brief */ }
}

/* ---------- footer attribution (verbatim, from data) ---------- */

function renderFooter(meta) {
  const attr = (meta && meta.attribution) || {};
  document.getElementById('attrTreasury').textContent = attr.us_treasury || '';
  document.getElementById('attrNyfed').textContent = attr.ny_fed || '';
  const bis = document.getElementById('attrBis');
  if (bis) {
    bis.textContent = attr.bis || '';
    bis.hidden = !attr.bis; // hide the block entirely when there's no BIS notice
  }
}

/* ---------- error state ---------- */

function showError(title, detail) {
  const dash = document.getElementById('dashboard');
  if (dash) dash.hidden = true;
  const box = document.getElementById('errorState');
  document.getElementById('errorTitle').textContent = title;
  document.getElementById('errorDetail').textContent = detail || '';
  box.hidden = false;
}

/* ---------- modal wiring (once) ---------- */

function wireModal() {
  const overlay = document.getElementById('overlay');
  const wrap = document.getElementById('sheetWrap');
  const sheet = document.getElementById('sheet');
  const close = document.getElementById('close');
  if (close) close.addEventListener('click', closeModal);
  if (wrap) wrap.addEventListener('click', (e) => { if (e.target === wrap) closeModal(); });
  if (sheet) sheet.addEventListener('click', (e) => e.stopPropagation());
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape' && overlay.classList.contains('open')) closeModal(); });
}

/* ---------- reveal-on-scroll + blob parallax (cosmetic) ---------- */

function wireCosmetics() {
  const io = new IntersectionObserver((es) => {
    es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
  }, { threshold: 0.12 });
  document.querySelectorAll('.reveal').forEach((elm) => io.observe(elm));

  const blobs = [].slice.call(document.querySelectorAll('.blob'));
  window.addEventListener('pointermove', (e) => {
    const x = e.clientX / window.innerWidth - 0.5, y = e.clientY / window.innerHeight - 0.5;
    blobs.forEach((b, i) => {
      const k = (i + 1) * 14;
      b.style.marginLeft = (x * k) + 'px';
      b.style.marginTop = (y * k) + 'px';
    });
  });
}

/* ---------- boot ---------- */

async function main() {
  wireModal();
  const result = await loadSnapshot();

  if (result.status === 'integrity-error') {
    showError('Integrità dei dati non verificata',
      'Lo snapshot non corrisponde al suo hash dichiarato. Rendering rifiutato per evitare dati corrotti o non aggiornati.');
    return;
  }
  if (result.status === 'schema-error') {
    showError('Versione dello schema non supportata',
      'schema_version = ' + String(result.schemaVersion) + ' (attesa 1).');
    return;
  }
  if (result.status !== 'ok') {
    showError('Dati non disponibili', result.message || 'Errore di caricamento.');
    return;
  }

  const snapshot = result.snapshot;
  try {
    renderTopBar(snapshot.meta);
    renderOverview(snapshot);
    renderSectors(snapshot);
    renderRates(snapshot);
    renderMovers(snapshot);
    renderHeadlines(snapshot);
    renderBrief(snapshot);
    renderFooter(snapshot.meta);
    wireCosmetics();
    wireViz();
  } catch (e) {
    showError('Errore di rendering', e && e.message ? e.message : String(e));
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', main);
} else {
  main();
}
