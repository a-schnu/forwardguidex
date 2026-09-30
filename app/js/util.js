/* ForwardGuidex — DOM + formatting helpers shared by every renderer.
 *
 * Security posture (unchanged from the single-file renderer):
 *   - Every data value reaches the DOM through textContent (el(), svg text) —
 *     never innerHTML. The only innerHTML writes in the app are the Morning
 *     Brief and chat replies, and only on DOMPurify-sanitised output.
 *   - External links go through makeSafeLink / setSafeExternalLink (https only).
 *   - Geometry and motion use attributes or CSSOM (style.setProperty), both
 *     permitted by the `style-src 'self'` CSP; no inline style="" strings.
 */

export const REDUCE = window.matchMedia('(prefers-reduced-motion: reduce)');
const SVGNS = 'http://www.w3.org/2000/svg';

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

export function svgEl(tag, attrs) {
  const node = document.createElementNS(SVGNS, tag);
  if (attrs) {
    for (const k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) node.setAttribute(k, attrs[k]);
    }
  }
  return node;
}

export function isNum(x) {
  return typeof x === 'number' && Number.isFinite(x);
}

export function arr(x) {
  return Array.isArray(x) ? x : [];
}

/* ---------- numbers ---------- */

const NF = new Map();
function nf(min, max) {
  const k = min + ':' + max;
  if (!NF.has(k)) {
    NF.set(k, new Intl.NumberFormat('it-IT', { minimumFractionDigits: min, maximumFractionDigits: max }));
  }
  return NF.get(k);
}

const MINUS = '−';

/** Price quote: 2 decimals, 4 below 10 so FX, copper, gas and XRP keep their precision. */
export function fmtPrice(x) {
  if (!isNum(x)) return '—';
  const d = Math.abs(x) < 10 ? 4 : 2;
  return nf(d, d).format(x);
}

export function fmtNum(x, digits) {
  const d = digits == null ? 2 : digits;
  return isNum(x) ? nf(d, d).format(x) : '—';
}

/** Yield / policy rate in percent: 2–3 decimals (3,625% must not print as 3,63%). */
export function fmtRate(x) {
  return isNum(x) ? nf(2, 3).format(x) + '%' : '—';
}

/** Signed percentage with a real minus sign. */
export function fmtPct(x) {
  if (!isNum(x)) return '—';
  const s = x > 0 ? '+' : x < 0 ? MINUS : '';
  return s + nf(2, 2).format(Math.abs(x)) + '%';
}

/** A change in percentage POINTS rendered as basis points (0.05 -> "+5 bp"). */
export function fmtBp(pp) {
  if (!isNum(pp)) return '—';
  const bp = Math.round(pp * 100);
  if (bp === 0) return '0 bp';
  return (bp > 0 ? '+' : MINUS) + Math.abs(bp) + ' bp';
}

export function signClass(x) {
  if (!isNum(x) || x === 0) return 'flat';
  return x > 0 ? 'up' : 'down';
}

/* ---------- dates ---------- */

function asDate(iso) {
  const d = iso instanceof Date ? iso : new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}

export function fmtDate(iso) {
  const d = asDate(iso);
  if (!d) return String(iso || '—');
  return new Intl.DateTimeFormat('it-IT', { dateStyle: 'medium', timeZone: 'UTC' }).format(d);
}

/** "lun 5 ott" — for agenda day chips and rows. */
export function fmtDay(iso) {
  const d = asDate(iso);
  if (!d) return String(iso || '—');
  return new Intl.DateTimeFormat('it-IT', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })
    .format(d).replace(/\./g, '');
}

export function fmtDateTime(iso) {
  const d = asDate(iso);
  if (!d) return String(iso || '—');
  return new Intl.DateTimeFormat('it-IT', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }).format(d) + ' UTC';
}

/** GDELT seendate "20260930T163000Z" -> Date (or null). */
export function parseSeendate(s) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(String(s || ''));
  if (!m) return null;
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
}

/** "12 min fa" / "3 h fa" / "2 g fa"; falls back to a date past a week. */
export function relTime(d, now) {
  if (!d) return '';
  const s = Math.round(((now || new Date()).getTime() - d.getTime()) / 1000);
  if (s < 60) return 'ora';
  if (s < 3600) return Math.floor(s / 60) + ' min fa';
  if (s < 86400) return Math.floor(s / 3600) + ' h fa';
  if (s < 7 * 86400) return Math.floor(s / 86400) + ' g fa';
  return fmtDate(d);
}

/* ---------- text ---------- */

/** Lower-case, accent-free form for search matching. */
export function normalize(s) {
  return String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

/* ---------- links ---------- */

function isHttps(url) {
  try { return new URL(url, location.href).protocol === 'https:'; } catch (_e) { return false; }
}

/** https-only anchor; anything else renders as inert text. */
export function makeSafeLink(text, url, className) {
  if (isHttps(url)) {
    const a = el('a', 'ext-link' + (className ? ' ' + className : ''), text);
    a.setAttribute('href', url);
    a.setAttribute('rel', 'noopener noreferrer');
    a.setAttribute('target', '_blank');
    return a;
  }
  return el('span', 'ext-link disabled' + (className ? ' ' + className : ''), text);
}

/** Harden an anchor from sanitised HTML: https gets rel/target, anything else becomes text. */
export function setSafeExternalLink(a) {
  const url = a.getAttribute('href') || '';
  if (isHttps(url)) {
    a.setAttribute('rel', 'noopener noreferrer');
    a.setAttribute('target', '_blank');
    return;
  }
  const span = el('span', 'ext-link disabled', a.textContent || url);
  if (a.parentNode) a.parentNode.replaceChild(span, a);
}

/* ---------- small UI pieces ---------- */

/** Coloured signed-percentage chip. */
export function pctChip(x, extra) {
  return el('span', 'chg ' + signClass(x) + (extra ? ' ' + extra : ''), fmtPct(x));
}

/**
 * Segmented control: buttons with aria-pressed. `onChange(value)` fires on a
 * change; returns { node, set(value) }.
 */
export function segmented(label, options, value, onChange) {
  const node = el('div', 'seg');
  node.setAttribute('role', 'group');
  node.setAttribute('aria-label', label);
  const btns = options.map((o) => {
    const b = el('button', null, o.label);
    b.type = 'button';
    if (o.title) b.title = o.title;
    b.setAttribute('aria-pressed', o.value === value ? 'true' : 'false');
    b.addEventListener('click', () => {
      if (b.getAttribute('aria-pressed') === 'true') return;
      set(o.value);
      onChange(o.value);
    });
    node.appendChild(b);
    return b;
  });
  function set(v) {
    btns.forEach((b, i) => b.setAttribute('aria-pressed', options[i].value === v ? 'true' : 'false'));
  }
  return { node, set };
}

/**
 * FLIP reorder: move `children` into `order` inside `parent`, animating each
 * node from its old position. Skipped under reduced motion.
 */
export function flipReorder(parent, order) {
  const first = new Map();
  if (!REDUCE.matches) order.forEach((n) => first.set(n, n.getBoundingClientRect()));
  order.forEach((n) => parent.appendChild(n));
  if (REDUCE.matches) return;
  const moved = [];
  order.forEach((n) => {
    const a = first.get(n);
    const b = n.getBoundingClientRect();
    const dx = a.left - b.left, dy = a.top - b.top;
    if (!dx && !dy) return;
    n.style.setProperty('transition', 'none');
    n.style.setProperty('transform', 'translate(' + dx + 'px,' + dy + 'px)');
    moved.push(n);
  });
  if (!moved.length) return;
  void parent.offsetWidth; // commit the inverted positions before animating back
  moved.forEach((n) => {
    n.style.setProperty('transition', 'transform .38s cubic-bezier(.2,.8,.2,1)');
    n.style.removeProperty('transform');
    setTimeout(() => n.style.removeProperty('transition'), 420);
  });
}

/**
 * Call `cb(width)` whenever `node`'s width settles on a meaningfully new value
 * (charts size their viewBox to it; a chart first drawn in a hidden tab has
 * width 0 and must redraw once it is laid out). Zero widths are ignored.
 */
export function onWidthChange(node, cb) {
  if (typeof ResizeObserver !== 'function') return;
  let last = Math.round(node.clientWidth);
  let t = null;
  new ResizeObserver((entries) => {
    const w = Math.round(entries[0].contentRect.width);
    if (!w || Math.abs(w - last) < 24) return;
    last = w;
    clearTimeout(t);
    t = setTimeout(() => cb(w), 80);
  }).observe(node);
}
