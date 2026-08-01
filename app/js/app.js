/* ForwardGuidex — dashboard renderer.
 *
 * Security posture:
 *   - Every scalar value from the snapshot is written via textContent (never innerHTML).
 *   - The ONLY innerHTML assignment is the Morning Brief, and only on DOMPurify-sanitised
 *     output of marked.parse(). External links go through setSafeExternalLink (https-only).
 *   - marked / DOMPurify are self-hosted globals loaded before this module.
 */

import { loadSnapshot, marketStateCosmetic } from './data.js';

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

/* ---------- top bar ---------- */

function renderTopBar(meta, freshnessDowngraded) {
  const badge = document.getElementById('badge');
  badge.textContent = meta.freshness + ' · ' + meta.quality;
  badge.classList.remove('is-fresh', 'is-stale', 'is-degraded');
  badge.classList.add(meta.freshness === 'FRESH' ? 'is-fresh' : 'is-stale');
  if (meta.quality !== 'OK') badge.classList.add('is-degraded');
  badge.title = freshnessDowngraded
    ? 'Freschezza declassata lato client per età dei dati'
    : 'Stato impostato dalla pipeline';

  document.getElementById('dataAsOf').textContent = fmtDateTime(meta.data_as_of);

  if (meta.is_demo) {
    const ribbon = document.getElementById('demoRibbon');
    ribbon.hidden = false;
  }

  // Cosmetic-only market indicator — never gates anything.
  try {
    const ms = marketStateCosmetic();
    const dot = document.getElementById('marketDot');
    const label = document.getElementById('marketLabel');
    if (label) label.textContent = ms.label;
    if (dot) dot.classList.toggle('open', !!ms.open);
  } catch (_e) { /* cosmetic only */ }
}

/* ---------- KPI strip ---------- */

function kpiCard(item) {
  const card = el('div', 'kpi glass');
  card.appendChild(el('div', 'kpi-name', item.name));
  const val = el('div', 'kpi-val');
  val.appendChild(el('span', 'kpi-last', fmtNum(item.last)));
  val.appendChild(el('span', 'kpi-ccy', item.currency || ''));
  card.appendChild(val);
  const rets = el('div', 'kpi-rets');
  rets.appendChild(el('span', 'ret ' + signClass(item.ret_1d), fmtPct(item.ret_1d)));
  const r5 = el('span', 'ret-5 muted', '5gg ' + fmtPct(item.ret_5d));
  rets.appendChild(r5);
  card.appendChild(rets);
  return card;
}

function renderKpis(snapshot) {
  const host = document.getElementById('kpis');
  host.textContent = '';
  const indices = Array.isArray(snapshot.indices) ? snapshot.indices : [];
  const futures = Array.isArray(snapshot.futures) ? snapshot.futures : [];
  indices.forEach((it) => host.appendChild(kpiCard(it)));
  futures.slice(0, 2).forEach((it) => host.appendChild(kpiCard(it)));
}

/* ---------- sectors (data-driven cards + zoom modal) ---------- */

const sectorsByKey = new Map();

function sectorCard(sector) {
  const card = el('div', 'scard glass');
  card.setAttribute('data-key', sector.key);

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
  body.appendChild(el('p', 's-why',
    'Media 1 giorno ' + fmtPct(sector.avg_ret_1d) + ' · Media 5 giorni ' + fmtPct(sector.avg_ret_5d)));

  const grid = el('div', 's-grid');
  grid.appendChild(itemsTable('ETF', sector.etfs));
  grid.appendChild(itemsTable('Titoli', sector.constituents));
  body.appendChild(grid);

  const overlay = document.getElementById('overlay');
  const wrap = document.getElementById('sheetWrap');
  const r = card.getBoundingClientRect();
  wrap.style.setProperty('--ox', (r.left + r.width / 2) + 'px');
  wrap.style.setProperty('--oy', (r.top + r.height / 2) + 'px');
  overlay.style.display = 'block';
  void overlay.offsetWidth; // force reflow so the zoom transition runs
  overlay.classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeModal() {
  const overlay = document.getElementById('overlay');
  overlay.classList.remove('open');
  document.body.style.overflow = '';
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
  cards.forEach((c) => c.addEventListener('click', () => { if (!moved) openSectorModal(c); }));
}

/* ---------- rates ---------- */

function renderRates(snapshot) {
  const host = document.getElementById('ratesStrip');
  host.textContent = '';
  (Array.isArray(snapshot.rates) ? snapshot.rates : []).forEach((r) => {
    const chip = el('div', 'rate glass');
    chip.appendChild(el('div', 'rate-name', r.name));
    const row = el('div', 'rate-row');
    row.appendChild(el('span', 'rate-val', fmtNum(r.value) + '%'));
    row.appendChild(el('span', 'rate-chg ' + signClass(r.chg), fmtPct(r.chg)));
    chip.appendChild(row);
    const meta = el('div', 'rate-meta');
    meta.appendChild(el('span', 'src', r.source));
    meta.appendChild(el('span', 'asof', fmtDate(r.as_of)));
    chip.appendChild(meta);
    host.appendChild(chip);
  });
}

/* ---------- movers ---------- */

function moverRow(item) {
  const row = el('div', 'mv-row');
  row.appendChild(el('span', 'mv-tk', item.ticker));
  row.appendChild(el('span', 'mv-nm', item.name));
  const s = el('span', 'mv-sec muted', item.sector || '');
  row.appendChild(s);
  row.appendChild(el('span', 'mv-ret ' + signClass(item.ret_1d), fmtPct(item.ret_1d)));
  return row;
}

function renderMovers(snapshot) {
  const movers = snapshot.movers || {};
  const g = document.getElementById('moversGainers');
  const l = document.getElementById('moversLosers');
  g.textContent = '';
  l.textContent = '';
  (movers.gainers || []).forEach((it) => g.appendChild(moverRow(it)));
  (movers.losers || []).forEach((it) => l.appendChild(moverRow(it)));
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
    brief.created_at ? 'Generato ' + fmtDateTime(brief.created_at) : '';

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
}

/* ---------- footer attribution (verbatim, from data) ---------- */

function renderFooter(meta) {
  const attr = (meta && meta.attribution) || {};
  document.getElementById('attrTreasury').textContent = attr.us_treasury || '';
  document.getElementById('attrNyfed').textContent = attr.ny_fed || '';
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
    renderTopBar(snapshot.meta, result.freshnessDowngraded);
    renderKpis(snapshot);
    renderSectors(snapshot);
    renderRates(snapshot);
    renderMovers(snapshot);
    renderHeadlines(snapshot);
    renderBrief(snapshot);
    renderFooter(snapshot.meta);
    wireCosmetics();
  } catch (e) {
    showError('Errore di rendering', e && e.message ? e.message : String(e));
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', main);
} else {
  main();
}
