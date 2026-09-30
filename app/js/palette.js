/* ForwardGuidex — search palette ("/" or Ctrl/⌘+K).
 *
 * Searches every instrument in the snapshot (indices, futures, ETF, crypto,
 * sector constituents), the sectors and the page sections. Results are built
 * with textContent; the query never touches the DOM as HTML.
 */

import { el, normalize, fmtPct, signClass } from './util.js';

let entries = [];
let results = [];
let sel = 0;
let lastFocus = null;

const $ = (id) => document.getElementById(id);

/** entries: [{ kind, label, sub, ticker?, ret?, run() }] */
export function initPalette(list) {
  entries = list.map((e) => Object.assign({}, e, {
    _l: normalize(e.label), _t: normalize(e.ticker || ''), _s: normalize(e.sub || '')
  }));
  const input = $('palInput');
  input.addEventListener('input', () => { sel = 0; search(input.value); });
  input.addEventListener('keydown', onKey);
  $('palette').addEventListener('click', (e) => { if (e.target === $('palette')) closePalette(); });

  window.addEventListener('keydown', (e) => {
    const k = e.key;
    const typing = e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || (e.target && e.target.isContentEditable);
    if ((k === 'k' || k === 'K') && (e.ctrlKey || e.metaKey)) { e.preventDefault(); togglePalette(); return; }
    if (k === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); openPalette(); }
  });
  $('searchBtn').addEventListener('click', openPalette);
}

function score(e, q) {
  if (!q) return e.kind === 'Sezione' ? 1 : 0;
  if (e._t && e._t === q) return 100;
  if (e._t && e._t.startsWith(q)) return 80;
  if (e._l.startsWith(q)) return 70;
  if (e._l.indexOf(' ' + q) !== -1) return 55;
  if (e._l.indexOf(q) !== -1) return 40;
  if (e._t && e._t.indexOf(q) !== -1) return 35;
  if (e._s.indexOf(q) !== -1) return 20;
  return 0;
}

function search(raw) {
  const q = normalize(raw).trim();
  results = entries
    .map((e) => ({ e, s: score(e, q) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s)
    .slice(0, 12)
    .map((x) => x.e);
  paint();
}

function paint() {
  const list = $('palList');
  list.textContent = '';
  $('palEmpty').hidden = results.length > 0;
  results.forEach((e, i) => {
    const li = el('li', 'pal-item' + (i === sel ? ' on' : ''));
    li.id = 'pal-opt-' + i;
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', i === sel ? 'true' : 'false');
    li.appendChild(el('span', 'pal-kind', e.kind));
    if (e.ticker) li.appendChild(el('span', 'tk', e.ticker));
    li.appendChild(el('span', 'pal-label', e.label));
    if (e.sub) li.appendChild(el('span', 'pal-sub', e.sub));
    if (typeof e.ret === 'number') li.appendChild(el('span', 'chg ' + signClass(e.ret), fmtPct(e.ret)));
    li.addEventListener('mousemove', () => { if (sel !== i) { sel = i; mark(); } });
    li.addEventListener('click', () => choose(i));
    list.appendChild(li);
  });
  $('palInput').setAttribute('aria-activedescendant', results.length ? 'pal-opt-' + sel : '');
}

function mark() {
  [].slice.call($('palList').children).forEach((li, i) => {
    li.classList.toggle('on', i === sel);
    li.setAttribute('aria-selected', i === sel ? 'true' : 'false');
    if (i === sel) li.scrollIntoView({ block: 'nearest' });
  });
  $('palInput').setAttribute('aria-activedescendant', results.length ? 'pal-opt-' + sel : '');
}

function onKey(e) {
  if (e.key === 'ArrowDown') { e.preventDefault(); if (results.length) { sel = (sel + 1) % results.length; mark(); } }
  else if (e.key === 'ArrowUp') { e.preventDefault(); if (results.length) { sel = (sel - 1 + results.length) % results.length; mark(); } }
  else if (e.key === 'Enter') { e.preventDefault(); choose(sel); }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePalette(); }
  else if (e.key === 'Tab') { e.preventDefault(); } // the input is the palette's only stop
}

function choose(i) {
  const e = results[i];
  if (!e) return;
  closePalette(true);
  e.run();
}

export function openPalette() {
  const p = $('palette');
  if (!p.hidden) return;
  lastFocus = document.activeElement;
  p.hidden = false;
  document.body.classList.add('layer-open');
  const input = $('palInput');
  input.value = '';
  sel = 0;
  search('');
  input.focus();
}

export function closePalette(keepFocusWhereItGoes) {
  const p = $('palette');
  if (p.hidden) return;
  p.hidden = true;
  if (!document.body.classList.contains('drawer-open')) document.body.classList.remove('layer-open');
  if (!keepFocusWhereItGoes && lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
  lastFocus = null;
}

function togglePalette() {
  if ($('palette').hidden) openPalette(); else closePalette();
}
