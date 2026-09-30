/* ForwardGuidex — inline-SVG charts (CSP-safe: no external libraries).
 *
 * Built with createElementNS; geometry via attributes, motion via CSS classes.
 * Tooltips are sibling <div>s positioned with style.setProperty and filled via
 * textContent.
 */

import { el, svgEl, isNum, arr, signClass, fmtPct, fmtRate, fmtBp, REDUCE } from './util.js';

function pathD(pts) {
  return 'M' + pts.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' L');
}

function polyLen(pts) {
  let len = 0;
  for (let i = 1; i < pts.length; i++) len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  return Math.ceil(len);
}

function drawIn(path, len) {
  path.setAttribute('stroke-dasharray', String(len));
  path.style.setProperty('--len', String(len));
  if (!REDUCE.matches) path.classList.add('draw');
}

/* ---------- sparkline (tiles, tape-free) ---------- */

/** Small trend line; colour follows the line's own direction. Null when < 2 points. */
export function sparkline(values, opts) {
  const v = arr(values).filter(isNum);
  if (v.length < 2) return null;
  const W = (opts && opts.w) || 120, H = (opts && opts.h) || 30, pad = 2;
  const lo = Math.min.apply(null, v), hi = Math.max.apply(null, v);
  const span = (hi - lo) || 1;
  const pts = v.map((y, i) => [pad + (i / (v.length - 1)) * (W - 2 * pad), pad + (1 - (y - lo) / span) * (H - 2 * pad)]);
  const line = pathD(pts);
  const area = line + ' L' + (W - pad) + ',' + H + ' L' + pad + ',' + H + ' Z';
  const svg = svgEl('svg', {
    'class': 'spark ' + signClass(v[v.length - 1] - v[0]),
    viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none', 'aria-hidden': 'true'
  });
  svg.appendChild(svgEl('path', { 'class': 'spark-area', d: area }));
  svg.appendChild(svgEl('path', { 'class': 'spark-line', d: line }));
  return svg;
}

/* ---------- interactive price chart (detail drawer) ---------- */

/** viewBox width = the host's CSS width, so axis text stays ~11px on a phone. */
function fitWidth(width, lo, hi) {
  return Math.round(Math.max(lo, Math.min(hi, width || hi)));
}

/**
 * @param {number[]} values  closes, oldest -> newest
 * @param {{fmt:(n:number)=>string, label:(i:number,n:number)=>string, aria:string, width?:number}} o
 */
export function priceChart(values, o) {
  const v = arr(values).filter(isNum);
  if (v.length < 2) return null;
  const W = fitWidth(o.width, 300, 640), H = W < 480 ? 210 : 250, m = { l: 4, r: 70, t: 14, b: 26 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  let lo = Math.min.apply(null, v), hi = Math.max.apply(null, v);
  const pad = ((hi - lo) || Math.abs(hi) * 0.01 || 1) * 0.14;
  lo -= pad; hi += pad;
  const n = v.length;
  const last = v[n - 1];
  const xAt = (i) => m.l + (i / (n - 1)) * pw;
  const yAt = (y) => m.t + (1 - (y - lo) / (hi - lo)) * ph;
  const pts = v.map((y, i) => [xAt(i), yAt(y)]);

  const host = el('div', 'pc ' + signClass(last - v[0]));
  const svg = svgEl('svg', {
    'class': 'pc-svg', viewBox: '0 0 ' + W + ' ' + H, role: 'img', tabindex: '0',
    'aria-label': o.aria + ' — frecce sinistra/destra per scorrere i punti'
  });

  for (let i = 0; i <= 4; i++) {
    const y = lo + ((hi - lo) * i) / 4;
    const yy = yAt(y);
    svg.appendChild(svgEl('line', { 'class': 'pc-grid', x1: m.l, x2: W - m.r, y1: yy.toFixed(1), y2: yy.toFixed(1) }));
    const t = svgEl('text', { 'class': 'pc-axis', x: W - m.r + 10, y: (yy + 4).toFixed(1) });
    t.textContent = o.fmt(y);
    svg.appendChild(t);
  }

  const line = pathD(pts);
  svg.appendChild(svgEl('path', {
    'class': 'pc-area',
    d: line + ' L' + xAt(n - 1).toFixed(1) + ',' + (m.t + ph) + ' L' + m.l + ',' + (m.t + ph) + ' Z'
  }));
  const lineEl = svgEl('path', { 'class': 'pc-line', d: line });
  drawIn(lineEl, polyLen(pts));
  svg.appendChild(lineEl);

  // last-close reference: dashed rule + value pill on the axis, like a terminal chart
  const ly = yAt(last);
  svg.appendChild(svgEl('line', { 'class': 'pc-last', x1: m.l, x2: W - m.r, y1: ly.toFixed(1), y2: ly.toFixed(1) }));
  svg.appendChild(svgEl('rect', { 'class': 'pc-pill', x: W - m.r + 2, y: (ly - 10).toFixed(1), width: m.r - 4, height: 20, rx: 4 }));
  const lt = svgEl('text', { 'class': 'pc-pill-t', x: W - m.r + 10, y: (ly + 4).toFixed(1) });
  lt.textContent = o.fmt(last);
  svg.appendChild(lt);

  [[0, 'start'], [n - 1, 'end']].forEach(([i, anchor]) => {
    const t = svgEl('text', { 'class': 'pc-axis', x: xAt(i).toFixed(1), y: H - 6, 'text-anchor': anchor });
    t.textContent = o.label(i, n);
    svg.appendChild(t);
  });

  const cross = svgEl('g', { 'class': 'pc-cross' });
  const vline = svgEl('line', { 'class': 'pc-vline', y1: m.t, y2: m.t + ph });
  const dot = svgEl('circle', { 'class': 'pc-dot', r: 4.5 });
  cross.appendChild(vline);
  cross.appendChild(dot);
  svg.appendChild(cross);

  const tip = el('div', 'pc-tip');
  tip.setAttribute('aria-live', 'polite');
  host.appendChild(svg);
  host.appendChild(tip);

  let cur = null;
  function show(i) {
    cur = Math.max(0, Math.min(n - 1, i));
    const [x, y] = pts[cur];
    vline.setAttribute('x1', x.toFixed(1));
    vline.setAttribute('x2', x.toFixed(1));
    dot.setAttribute('cx', x.toFixed(1));
    dot.setAttribute('cy', y.toFixed(1));
    cross.classList.add('on');
    tip.textContent = '';
    tip.appendChild(el('b', null, o.label(cur, n)));
    tip.appendChild(el('span', 'pc-tip-v', o.fmt(v[cur])));
    if (cur < n - 1 && v[cur]) {
      const ch = (last / v[cur] - 1) * 100;
      const row = el('span', 'pc-tip-d');
      row.appendChild(document.createTextNode('da qui a ultima '));
      row.appendChild(el('span', 'chg ' + signClass(ch), fmtPct(ch)));
      tip.appendChild(row);
    }
    tip.style.setProperty('left', ((x / W) * 100) + '%');
    tip.style.setProperty('top', ((y / H) * 100) + '%');
    tip.classList.toggle('flip', x / W > 0.62);
    tip.classList.add('on');
  }
  function hide() {
    cur = null;
    cross.classList.remove('on');
    tip.classList.remove('on');
  }
  svg.addEventListener('pointermove', (e) => {
    const r = svg.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * W;
    show(Math.round(((x - m.l) / pw) * (n - 1)));
  });
  svg.addEventListener('pointerleave', () => { if (document.activeElement !== svg) hide(); });
  svg.addEventListener('focus', () => show(n - 1));
  svg.addEventListener('blur', hide);
  svg.addEventListener('keydown', (e) => {
    let i = null;
    if (e.key === 'ArrowLeft') i = (cur == null ? n - 1 : cur) - 1;
    else if (e.key === 'ArrowRight') i = (cur == null ? n - 1 : cur) + 1;
    else if (e.key === 'Home') i = 0;
    else if (e.key === 'End') i = n - 1;
    if (i != null) { e.preventDefault(); e.stopPropagation(); show(i); }
  });
  return host;
}

/* ---------- yield curve ---------- */

/**
 * @param {{t:number,label:string,value:number,chg:number|null,name:string}[]} points  sorted by tenor
 * Log-scaled tenor axis (2A/5A/10A/30A would crowd the short end on a linear one).
 * Returns { node, setPrev(bool) } — setPrev toggles yesterday's curve (value - chg).
 */
export function yieldCurveChart(points, width) {
  const P = arr(points).filter((p) => isNum(p.t) && p.t > 0 && isNum(p.value));
  if (P.length < 2) return null;
  const W = fitWidth(width, 300, 720), H = W < 480 ? 220 : 260, m = { l: 48, r: 20, t: 18, b: 34 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const prevOf = (p) => (isNum(p.chg) ? p.value - p.chg : null);
  const all = P.map((p) => p.value).concat(P.map(prevOf).filter(isNum));
  let lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
  const pad = ((hi - lo) || 0.5) * 0.3;
  lo -= pad; hi += pad;
  const l0 = Math.log(P[0].t), l1 = Math.log(P[P.length - 1].t);
  const xAt = (t) => m.l + ((Math.log(t) - l0) / ((l1 - l0) || 1)) * pw;
  const yAt = (y) => m.t + (1 - (y - lo) / (hi - lo)) * ph;

  const host = el('div', 'yc');
  const svg = svgEl('svg', {
    'class': 'yc-svg', viewBox: '0 0 ' + W + ' ' + H, role: 'img', tabindex: '0',
    'aria-label': 'Curva dei rendimenti dei Treasury USA — frecce per scorrere le scadenze'
  });

  for (let i = 0; i <= 4; i++) {
    const y = lo + ((hi - lo) * i) / 4;
    const yy = yAt(y);
    svg.appendChild(svgEl('line', { 'class': 'pc-grid', x1: m.l, x2: W - m.r, y1: yy.toFixed(1), y2: yy.toFixed(1) }));
    const t = svgEl('text', { 'class': 'pc-axis', x: m.l - 8, y: (yy + 4).toFixed(1), 'text-anchor': 'end' });
    t.textContent = fmtRate(y).replace(/(,\d\d)\d%$/, '$1%');
    svg.appendChild(t);
  }

  const cur = P.map((p) => [xAt(p.t), yAt(p.value)]);
  const havePrev = P.every((p) => isNum(prevOf(p)));
  let prevLine = null;
  if (havePrev) {
    prevLine = svgEl('path', { 'class': 'yc-prev', d: pathD(P.map((p) => [xAt(p.t), yAt(prevOf(p))])) });
    svg.appendChild(prevLine);
  }
  svg.appendChild(svgEl('path', {
    'class': 'yc-area',
    d: pathD(cur) + ' L' + cur[cur.length - 1][0].toFixed(1) + ',' + (m.t + ph) + ' L' + cur[0][0].toFixed(1) + ',' + (m.t + ph) + ' Z'
  }));
  const lineEl = svgEl('path', { 'class': 'yc-line', d: pathD(cur) });
  drawIn(lineEl, polyLen(cur));
  svg.appendChild(lineEl);

  const vline = svgEl('line', { 'class': 'pc-vline yc-vline', y1: m.t, y2: m.t + ph });
  svg.appendChild(vline);
  const dots = P.map((p, i) => {
    const t = svgEl('text', { 'class': 'pc-axis yc-x', x: cur[i][0].toFixed(1), y: H - 10, 'text-anchor': 'middle' });
    t.textContent = p.label;
    svg.appendChild(t);
    const d = svgEl('circle', { 'class': 'yc-dot', cx: cur[i][0].toFixed(1), cy: cur[i][1].toFixed(1), r: 4 });
    svg.appendChild(d);
    return d;
  });

  const tip = el('div', 'pc-tip');
  tip.setAttribute('aria-live', 'polite');
  host.appendChild(svg);
  host.appendChild(tip);

  let sel = null;
  function show(i) {
    sel = Math.max(0, Math.min(P.length - 1, i));
    const p = P[sel];
    const [x, y] = cur[sel];
    vline.setAttribute('x1', x.toFixed(1));
    vline.setAttribute('x2', x.toFixed(1));
    vline.classList.add('on');
    dots.forEach((d, k) => d.classList.toggle('on', k === sel));
    tip.textContent = '';
    tip.appendChild(el('b', null, p.label));
    tip.appendChild(el('span', 'pc-tip-v', fmtRate(p.value)));
    if (isNum(p.chg)) {
      const row = el('span', 'pc-tip-d');
      row.appendChild(document.createTextNode('1g '));
      row.appendChild(el('span', 'chg ' + signClass(p.chg), fmtBp(p.chg)));
      tip.appendChild(row);
    }
    tip.style.setProperty('left', ((x / W) * 100) + '%');
    tip.style.setProperty('top', ((y / H) * 100) + '%');
    tip.classList.toggle('flip', x / W > 0.62);
    tip.classList.add('on');
  }
  function hide() {
    sel = null;
    vline.classList.remove('on');
    dots.forEach((d) => d.classList.remove('on'));
    tip.classList.remove('on');
  }
  svg.addEventListener('pointermove', (e) => {
    const r = svg.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    cur.forEach((c, i) => { if (Math.abs(c[0] - x) < Math.abs(cur[best][0] - x)) best = i; });
    show(best);
  });
  svg.addEventListener('pointerleave', () => { if (document.activeElement !== svg) hide(); });
  svg.addEventListener('focus', () => show(sel == null ? P.length - 1 : sel));
  svg.addEventListener('blur', hide);
  svg.addEventListener('keydown', (e) => {
    let i = null;
    if (e.key === 'ArrowLeft') i = (sel == null ? 0 : sel) - 1;
    else if (e.key === 'ArrowRight') i = (sel == null ? -1 : sel) + 1;
    if (i != null) { e.preventDefault(); show(i); }
  });

  return {
    node: host,
    hasPrev: havePrev,
    setPrev(on) { if (prevLine) prevLine.classList.toggle('on', !!on); }
  };
}

/* ---------- bars ---------- */

/** Horizontal bar around a centre baseline; positive grows right, negative left. */
export function divergingBar(value, maxAbs, cls) {
  const W = 200, H = 14, cx = W / 2;
  const frac = maxAbs > 0 ? Math.min(1, Math.abs(value) / maxAbs) : 0;
  const w = Math.max(frac * (cx - 1), 0.8);
  const pos = value >= 0;
  const svg = svgEl('svg', {
    'class': 'dbar ' + (cls || ''), viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none', 'aria-hidden': 'true'
  });
  svg.appendChild(svgEl('line', { 'class': 'dbar-zero', x1: cx, y1: 0, x2: cx, y2: H }));
  svg.appendChild(svgEl('rect', {
    'class': 'dbar-bar ' + signClass(value) + (pos ? ' pos' : ' neg'),
    x: (pos ? cx : cx - w).toFixed(1), y: 2, width: w.toFixed(1), height: H - 4, rx: 1.5
  }));
  return svg;
}

/** One-sided magnitude bar (movers). */
export function magnitudeBar(value, maxAbs) {
  const W = 100, H = 4;
  const frac = maxAbs > 0 ? Math.min(1, Math.abs(value) / maxAbs) : 0;
  const svg = svgEl('svg', { 'class': 'mbar', viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
  svg.appendChild(svgEl('rect', { 'class': 'mbar-track', x: 0, y: 0, width: W, height: H, rx: 2 }));
  svg.appendChild(svgEl('rect', {
    'class': 'mbar-bar ' + signClass(value), x: 0, y: 0, width: Math.max(frac * W, 1).toFixed(1), height: H, rx: 2
  }));
  return svg;
}
