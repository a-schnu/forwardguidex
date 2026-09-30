/* ForwardGuidex — detail drawer (instrument / sector / Morning Brief).
 *
 * One right-hand panel with a view stack, so a sector can open one of its
 * stocks and "Indietro" returns to the sector. Views render from the in-memory
 * snapshot via the registry app.js hands to initDrawer(); nothing is fetched.
 */

import {
  el, isNum, arr, fmtPrice, fmtPct, fmtDate, fmtDay, signClass, pctChip, segmented,
  makeSafeLink, normalize, onWidthChange, REDUCE
} from './util.js';
import { priceChart, divergingBar } from './charts.js';
import { askAssistant } from './chat.js';

let ctx = null;          // { instruments, sectors, snapshot, briefNode, topicLabel }
const stack = [];        // [{ type, key, list }]
let lastFocus = null;
let closeTimer = null;   // pending hide after the exit transition

const $ = (id) => document.getElementById(id);

export function initDrawer(context) {
  ctx = context;
  $('drawerClose').addEventListener('click', closeDrawer);
  $('drawerBack').addEventListener('click', back);
  $('scrim').addEventListener('click', closeDrawer);
  $('drawer').addEventListener('keydown', onKey);
}

export function isDrawerOpen() {
  return $('drawer').classList.contains('in');
}

/** What the drawer shows, for the chat grounding context. */
export function drawerContext() {
  const top = stack[stack.length - 1];
  if (!top || !isDrawerOpen()) return '';
  if (top.type === 'instrument') {
    const rec = ctx.instruments.get(top.key);
    if (rec) return 'L\'utente sta guardando il dettaglio di ' + rec.item.name + ' (' + rec.item.ticker + ').';
  }
  if (top.type === 'sector') {
    const s = ctx.sectors.get(top.key);
    if (s) return 'L\'utente sta guardando il dettaglio del settore ' + s.label + '.';
  }
  if (top.type === 'brief') return 'L\'utente sta leggendo il Morning Brief.';
  return '';
}

/** Open a view. `list` (tickers) enables prev/next stepping; `push` keeps history. */
export function openView(view, opts) {
  const push = opts && opts.push;
  if (!push) stack.length = 0;
  stack.push(view);
  show();      // first, so the body has a width for the chart to fit
  render();
}

export function openInstrument(ticker, list, opts) {
  if (!ctx.instruments.has(ticker)) return;
  openView({ type: 'instrument', key: ticker, list: list || null }, opts);
}

export function openSector(key, opts) {
  if (!ctx.sectors.has(key)) return;
  openView({ type: 'sector', key: key }, opts);
}

export function openBrief() {
  openView({ type: 'brief' });
}

function show() {
  const d = $('drawer');
  // Reopening during the exit transition must cancel the pending hide, or
  // the panel slides in and is then hidden by the old timer.
  if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
  if (d.classList.contains('in')) return;
  if (d.hidden) lastFocus = document.activeElement;
  d.hidden = false;
  $('scrim').hidden = false;
  document.body.classList.add('layer-open', 'drawer-open');
  // Force a style flush so the slide-in transitions from the off-screen state.
  // (Not rAF: it does not fire in background tabs, which left the panel stuck.)
  void d.offsetWidth;
  d.classList.add('in');
  $('scrim').classList.add('in');
  $('drawerClose').focus();
}

export function closeDrawer() {
  const d = $('drawer');
  if (!d.classList.contains('in')) return;
  d.classList.remove('in');
  $('scrim').classList.remove('in');
  document.body.classList.remove('drawer-open');
  if (!document.querySelector('.palette:not([hidden])')) document.body.classList.remove('layer-open');
  const done = () => { closeTimer = null; d.hidden = true; $('scrim').hidden = true; };
  if (REDUCE.matches) done(); else closeTimer = setTimeout(done, 240);
  stack.length = 0;
  markActive(null);
  if (lastFocus && typeof lastFocus.focus === 'function' && document.contains(lastFocus)) lastFocus.focus();
  lastFocus = null;
}

function back() {
  if (stack.length > 1) {
    stack.pop();
    render();
  }
}

function step(delta) {
  const top = stack[stack.length - 1];
  if (!top || top.type !== 'instrument' || !top.list) return;
  const i = top.list.indexOf(top.key);
  if (i === -1) return;
  const next = top.list[(i + delta + top.list.length) % top.list.length];
  top.key = next;
  render();
}

/** Keep Tab inside the dialog (it is aria-modal). */
function trapTab(e, root) {
  const f = [].slice.call(root.querySelectorAll('button, a[href], [tabindex]:not([tabindex="-1"])'))
    .filter((n) => !n.hidden && n.offsetParent !== null);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

function onKey(e) {
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeDrawer(); return; }
  if (e.key === 'Tab') { trapTab(e, $('drawer')); return; }
  const inField = e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName);
  const onChart = e.target && e.target.closest && e.target.closest('svg');
  if (inField || onChart) return;
  if (e.key === 'ArrowRight' || e.key === 'j') { e.preventDefault(); step(1); }
  else if (e.key === 'ArrowLeft' || e.key === 'k') { e.preventDefault(); step(-1); }
  else if (e.key === 'Backspace') { e.preventDefault(); back(); }
}

/** Highlight the tile / row for the instrument on screen. */
function markActive(ticker) {
  document.querySelectorAll('[data-tk].is-active').forEach((n) => n.classList.remove('is-active'));
  if (!ticker) return;
  document.querySelectorAll('[data-tk="' + CSS.escape(ticker) + '"]').forEach((n) => n.classList.add('is-active'));
}

function render() {
  const top = stack[stack.length - 1];
  const body = $('drawerBody');
  body.textContent = '';
  body.scrollTop = 0;
  $('drawerBack').hidden = stack.length < 2;
  const nav = $('drawerNav');
  nav.textContent = '';
  if (!top) return;

  let title = '';
  if (top.type === 'instrument') title = renderInstrument(body, top, nav);
  else if (top.type === 'sector') title = renderSector(body, top);
  else if (top.type === 'brief') title = renderBrief(body);
  $('drawer').setAttribute('aria-label', title || 'Dettaglio');
  markActive(top.type === 'instrument' ? top.key : null);
  body.classList.remove('enter');
  if (!REDUCE.matches) { void body.offsetWidth; body.classList.add('enter'); }
}

/* ---------- instrument ---------- */

/* 'max' = the whole history the snapshot carries (30 closes in production). */
let period = 'max';
function periodOptions(n) {
  return [5, 10].filter((x) => x < n).map((x) => ({ value: x, label: x + ' gg' }))
    .concat([{ value: 'max', label: n + ' gg' }]);
}

function stat(label, value, cls) {
  const d = el('div', 'st');
  d.appendChild(el('span', 'st-l', label));
  d.appendChild(el('span', 'st-v ' + (cls || ''), value));
  return d;
}

/** Annualised volatility of daily log returns (crypto trades every day). */
function annualVol(v, days) {
  if (v.length < 8) return null;
  const r = [];
  for (let i = 1; i < v.length; i++) if (v[i - 1] > 0 && v[i] > 0) r.push(Math.log(v[i] / v[i - 1]));
  if (r.length < 6) return null;
  const mean = r.reduce((a, b) => a + b, 0) / r.length;
  const sd = Math.sqrt(r.reduce((a, b) => a + (b - mean) * (b - mean), 0) / (r.length - 1));
  return sd * Math.sqrt(days) * 100;
}

function renderInstrument(body, view, nav) {
  const rec = ctx.instruments.get(view.key);
  if (!rec) return '';
  const it = rec.item;

  if (view.list && view.list.length > 1) {
    const i = view.list.indexOf(view.key);
    const prev = el('button', 'icon-btn', '‹');
    prev.type = 'button';
    prev.setAttribute('aria-label', 'Strumento precedente');
    prev.title = 'Precedente (←)';
    prev.addEventListener('click', () => step(-1));
    const next = el('button', 'icon-btn', '›');
    next.type = 'button';
    next.setAttribute('aria-label', 'Strumento successivo');
    next.title = 'Successivo (→)';
    next.addEventListener('click', () => step(1));
    nav.appendChild(prev);
    nav.appendChild(el('span', 'dn-pos', (i + 1) + ' / ' + view.list.length));
    nav.appendChild(next);
  }

  const head = el('div', 'dh');
  const tags = el('div', 'dh-tags');
  tags.appendChild(el('span', 'tk', it.ticker));
  tags.appendChild(el('span', 'tag', rec.kind));
  if (rec.sector) {
    const s = ctx.sectors.get(rec.sector);
    const b = el('button', 'tag tag-link', s ? s.label : rec.sector);
    b.type = 'button';
    b.title = 'Apri il settore';
    b.addEventListener('click', () => openSector(rec.sector, { push: true }));
    tags.appendChild(b);
  }
  head.appendChild(tags);
  head.appendChild(el('h2', 'dh-title', it.name || it.ticker));

  const px = el('div', 'dh-px');
  px.appendChild(el('span', 'dh-last num', fmtPrice(it.last)));
  px.appendChild(el('span', 'dh-ccy', it.currency || ''));
  px.appendChild(pctChip(it.ret_1d, 'lg'));
  px.appendChild(el('span', 'dh-5d muted', '5g ' + fmtPct(it.ret_5d)));
  head.appendChild(px);
  if (rec.kind !== 'Indice' && it.currency && it.currency !== 'EUR' && isNum(it.eur)) {
    head.appendChild(el('div', 'dh-eur muted', '≈ ' + fmtPrice(it.eur) + ' EUR'));
  }
  body.appendChild(head);

  const spark = arr(it.spark).filter(isNum);
  if (spark.length >= 2) {
    const chartBox = el('div', 'd-chart');
    const statsBox = el('div', 'stats');
    const range = el('div', 'range');
    const draw = () => {
      const v = period === 'max' ? spark : spark.slice(-Math.min(period, spark.length));
      chartBox.textContent = '';
      const chart = priceChart(v, {
        width: chartBox.clientWidth,
        fmt: fmtPrice,
        label: (i, n) => (i === n - 1 ? 'ultima' : '−' + (n - 1 - i) + ' gg'),
        aria: 'Andamento di ' + (it.name || it.ticker) + ', ultime ' + v.length + ' sedute'
      });
      if (chart) chartBox.appendChild(chart);

      const hi = Math.max.apply(null, v), lo = Math.min.apply(null, v);
      const last = v[v.length - 1];
      const chg = (last / v[0] - 1) * 100;
      statsBox.textContent = '';
      statsBox.appendChild(stat('Var. ' + v.length + ' gg', fmtPct(chg), signClass(chg)));
      statsBox.appendChild(stat('Massimo', fmtPrice(hi)));
      statsBox.appendChild(stat('Minimo', fmtPrice(lo)));
      statsBox.appendChild(stat('Dal massimo', fmtPct((last / hi - 1) * 100), signClass(last - hi)));
      const vol = annualVol(v, rec.kind === 'Crypto' ? 365 : 252);
      if (vol != null) statsBox.appendChild(stat('Vol. annua', fmtPct(vol).replace('+', '')));

      range.textContent = '';
      const pos = hi > lo ? (last - lo) / (hi - lo) : 0.5;
      range.appendChild(el('span', 'range-l num', fmtPrice(lo)));
      const track = el('div', 'range-t');
      const mark = el('span', 'range-m');
      mark.style.setProperty('--pos', String(pos));
      track.appendChild(mark);
      range.appendChild(track);
      range.appendChild(el('span', 'range-l num', fmtPrice(hi)));
      range.setAttribute('aria-label', 'Posizione nel range ' + v.length + ' gg: ' + Math.round(pos * 100) + '%');
    };
    const bar = el('div', 'd-bar');
    bar.appendChild(el('span', 'd-bar-l', 'Andamento'));
    const opts = periodOptions(spark.length);
    if (!opts.some((o) => o.value === period)) period = 'max';
    bar.appendChild(segmented('Periodo', opts, period, (val) => { period = val; draw(); }).node);
    body.appendChild(bar);
    body.appendChild(chartBox);
    body.appendChild(range);
    body.appendChild(statsBox);
    draw();
    onWidthChange(chartBox, () => { if (chartBox.isConnected) draw(); });
  }

  // Related: earnings, filings, headlines — only what the snapshot actually has.
  const snap = ctx.snapshot;
  const earn = arr(snap.earnings).filter((e) => e.ticker === it.ticker);
  const trig = arr(snap.triggers).filter((t) => t.ticker === it.ticker);
  const news = relatedHeadlines(it, rec);
  if (earn.length || trig.length || news.length) {
    const sec = el('div', 'd-sec');
    sec.appendChild(el('h3', 'd-h', 'Collegati'));
    earn.forEach((e) => {
      const row = el('div', 'rel');
      row.appendChild(el('span', 'rel-k', 'Trimestrale'));
      row.appendChild(el('span', 'rel-t', fmtDay(e.date) + (isNum(e.eps_estimate) ? ' · EPS stimato ' + fmtPrice(e.eps_estimate) : '')));
      sec.appendChild(row);
    });
    trig.slice(0, 3).forEach((t) => {
      const row = el('div', 'rel');
      row.appendChild(el('span', 'rel-k', t.kind === 'sec_8k' ? '8-K' : 'EO'));
      const tt = el('span', 'rel-t');
      tt.appendChild(makeSafeLink(t.title, t.url));
      tt.appendChild(el('span', 'muted', ' · ' + fmtDate(t.date)));
      row.appendChild(tt);
      sec.appendChild(row);
    });
    news.slice(0, 4).forEach((h) => {
      const row = el('div', 'rel');
      row.appendChild(el('span', 'rel-k', ctx.topicLabel(h.topic)));
      const tt = el('span', 'rel-t');
      tt.appendChild(makeSafeLink(h.title, h.url));
      row.appendChild(tt);
      sec.appendChild(row);
    });
    body.appendChild(sec);
  }

  body.appendChild(askRow('Chiedi all\'assistente su ' + (it.name || it.ticker),
    'Analizza ' + (it.name || it.ticker) + ' (' + it.ticker + '): movimento di oggi ' + fmtPct(it.ret_1d) +
    ', 5 giorni ' + fmtPct(it.ret_5d) + '. Cosa lo guida e cosa tenere d\'occhio?'));
  return it.name || it.ticker;
}

/* Futures have no name words that headlines use; map them to an economic topic. */
const TOPIC_BY_TICKER = {
  'CL=F': 'energia', 'BZ=F': 'energia', 'NG=F': 'energia', 'RB=F': 'energia',
  'ZN=F': 'debito_bond', 'ZB=F': 'debito_bond', 'TLT': 'debito_bond',
  '6E=F': 'banche_centrali', '6B=F': 'banche_centrali'
};
const STOP = new Set(['index', 'futures', 'future', 'trust', 'fund', 'group', 'holdings', 'class', 'corp',
  'corporation', 'company', 'the', 'and', 'inc', 'plc', 'ucits', 'etf', 'msci', 'ishares', 'xtrackers',
  'vanguard', 'spdr', 'invesco', 'world', 'total', 'market', 'bond', 'equal', 'weight', 'composite']);

function relatedHeadlines(it, rec) {
  const topic = TOPIC_BY_TICKER[it.ticker];
  const words = normalize(it.name).split(/[^a-z0-9&]+/).filter((w) => w.length >= 4 && !STOP.has(w));
  const tk = /^[A-Z]{3,5}$/.test(it.ticker) ? it.ticker.toLowerCase() : null;
  if (tk) words.push(tk);
  return arr(ctx.snapshot.headlines).filter((h) => {
    if (topic && h.topic === topic) return true;
    const t = ' ' + normalize(h.title).replace(/[^a-z0-9&]+/g, ' ') + ' ';
    return words.some((w) => t.indexOf(' ' + w + ' ') !== -1);
  });
}

function askRow(label, question) {
  const b = el('button', 'ask', label);
  b.type = 'button';
  b.addEventListener('click', () => {
    closeDrawer();
    askAssistant(question);
  });
  const row = el('div', 'd-ask');
  row.appendChild(b);
  row.appendChild(el('span', 'muted', 'La domanda viene preparata, non inviata.'));
  return row;
}

/* ---------- sector ---------- */

function renderSector(body, view) {
  const s = ctx.sectors.get(view.key);
  if (!s) return '';
  const head = el('div', 'dh');
  const tags = el('div', 'dh-tags');
  tags.appendChild(el('span', 'tag', 'Settore'));
  tags.appendChild(el('span', 'tag', arr(s.constituents).length + ' titoli · ' + arr(s.etfs).length + ' ETF'));
  head.appendChild(tags);
  head.appendChild(el('h2', 'dh-title', s.label));
  const px = el('div', 'dh-px');
  px.appendChild(pctChip(s.avg_ret_1d, 'lg'));
  px.appendChild(el('span', 'dh-5d muted', '5g ' + fmtPct(s.avg_ret_5d)));
  head.appendChild(px);
  head.appendChild(el('div', 'dh-eur muted', 'Media semplice dei rendimenti dei titoli del paniere.'));
  body.appendChild(head);

  const list = (items, title) => {
    const rows = arr(items).filter((x) => x && x.ticker);
    if (!rows.length) return;
    const sorted = rows.slice().sort((a, b) => (isNum(b.ret_1d) ? b.ret_1d : -1e9) - (isNum(a.ret_1d) ? a.ret_1d : -1e9));
    const maxAbs = Math.max.apply(null, sorted.map((x) => Math.abs(x.ret_1d) || 0).concat(0.01));
    const tickers = sorted.map((x) => x.ticker);
    const sec = el('div', 'd-sec');
    sec.appendChild(el('h3', 'd-h', title));
    const tbl = el('div', 'clist');
    sorted.forEach((x) => {
      const row = el('button', 'crow');
      row.type = 'button';
      row.setAttribute('data-tk', x.ticker);
      row.appendChild(el('span', 'tk', x.ticker));
      row.appendChild(el('span', 'crow-n', x.name || ''));
      const bar = el('span', 'crow-b');
      bar.appendChild(divergingBar(isNum(x.ret_1d) ? x.ret_1d : 0, maxAbs));
      row.appendChild(bar);
      row.appendChild(el('span', 'crow-p num', fmtPrice(x.last)));
      row.appendChild(pctChip(x.ret_1d));
      row.addEventListener('click', () => openInstrument(x.ticker, tickers, { push: true }));
      tbl.appendChild(row);
    });
    sec.appendChild(tbl);
    body.appendChild(sec);
  };
  list(s.constituents, 'Titoli');
  list(s.etfs, 'ETF');

  body.appendChild(askRow('Chiedi all\'assistente sul settore',
    'Settore ' + s.label + ': oggi ' + fmtPct(s.avg_ret_1d) + ', 5 giorni ' + fmtPct(s.avg_ret_5d) +
    '. Quali titoli guidano il movimento e perché?'));
  return s.label;
}

/* ---------- brief ---------- */

function renderBrief(body) {
  const head = el('div', 'dh');
  const tags = el('div', 'dh-tags');
  tags.appendChild(el('span', 'tag', 'Morning Brief'));
  tags.appendChild(el('span', 'tag tag-ai', 'Generato con AI'));
  head.appendChild(tags);
  const b = ctx.snapshot.brief || {};
  head.appendChild(el('div', 'dh-eur muted', b.created_at ? fmtDate(b.created_at) : ''));
  body.appendChild(head);
  if (ctx.briefNode) {
    const clone = ctx.briefNode.cloneNode(true); // already-sanitised DOM
    clone.classList.add('prose', 'd-prose');
    body.appendChild(clone);
  }
  return 'Morning Brief';
}
