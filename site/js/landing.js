/* ForwardGuidex — landing behaviour (extracted from inline <script> for CSP compliance).
   All styling is applied via CSSOM (element.style / setProperty) or classes — never via
   inline style="" strings in markup — so a strict `style-src 'self'` CSP is satisfied. */
(function () {
  'use strict';

  /* reveal-on-scroll */
  var io = new IntersectionObserver(function (es) {
    es.forEach(function (e) {
      if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
    });
  }, { threshold: 0.14 });
  document.querySelectorAll('.reveal').forEach(function (el, i) {
    el.style.transitionDelay = (i % 4 * 60) + 'ms';
    io.observe(el);
  });

  /* segmented control */
  var thumb = document.getElementById('thumb');
  function moveThumb(btn) {
    if (!btn || !thumb) return;
    thumb.style.width = btn.offsetWidth + 'px';
    thumb.style.transform = 'translateX(' + (btn.offsetLeft - 6) + 'px)';
  }
  document.querySelectorAll('.seg').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.seg').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      moveThumb(btn);
      document.querySelectorAll('.pane').forEach(function (p) { p.classList.remove('active'); });
      document.getElementById('pane-' + btn.dataset.pane).classList.add('active');
    });
  });
  window.addEventListener('load', function () { moveThumb(document.querySelector('.seg.active')); });

  /* sector track + dots */
  var track = document.getElementById('track');
  var cards = track ? [].slice.call(track.children) : [];
  var dots = document.getElementById('dots');
  cards.forEach(function (_, i) {
    var b = document.createElement('button');
    b.className = 'dot' + (i === 0 ? ' active' : '');
    b.addEventListener('click', function () {
      track.scrollTo({ left: cards[i].offsetLeft - cards[0].offsetLeft, behavior: 'smooth' });
    });
    dots.appendChild(b);
  });
  var dotEls = dots ? [].slice.call(dots.children) : [];
  function step() { return cards.length > 1 ? cards[1].offsetLeft - cards[0].offsetLeft : 1; }
  if (track) {
    track.addEventListener('scroll', function () {
      var i = Math.max(0, Math.min(cards.length - 1, Math.round(track.scrollLeft / step())));
      dotEls.forEach(function (d, k) { d.classList.toggle('active', k === i); });
    });
  }

  /* drag-to-scroll */
  var down = false, moved = false, sx = 0, sl = 0;
  if (track) {
    track.addEventListener('pointerdown', function (e) { down = true; moved = false; sx = e.pageX; sl = track.scrollLeft; });
    window.addEventListener('pointermove', function (e) {
      if (!down) return;
      if (Math.abs(e.pageX - sx) > 6) { moved = true; track.classList.add('drag'); }
      track.scrollLeft = sl - (e.pageX - sx);
    });
    window.addEventListener('pointerup', function () { down = false; track.classList.remove('drag'); });
  }

  /* sector detail content (hardcoded showcase data) */
  var DETAILS = {
    energia: { tag: 'Oil & Gas', name: 'Energia', pct: '+1.8%', dir: 'up', why: 'Corre col greggio: OPEC+, scorte USA, Medio Oriente.',
      bars: [{ l: 'Brent', v: 1.9 }, { l: 'WTI', v: 2.0 }, { l: 'XLE', v: 1.8 }, { l: 'XOP', v: 2.3 }, { l: 'OIH', v: 1.1 }],
      rows: [['XOM', 'Exxon', '+2.1'], ['CVX', 'Chevron', '+1.6'], ['COP', 'Conoco', '+2.4'], ['SLB', 'Schlumberger', '+0.9']],
      watch: ['OPEC+ giovedì', 'Scorte EIA', 'Iran'] },
    difesa: { tag: 'Defense & Aerospace', name: 'Difesa', pct: '+2.4%', dir: 'up', why: 'Reagisce prima a guerre ed escalation. Budget in salita.',
      bars: [{ l: 'ITA', v: 2.4 }, { l: 'XAR', v: 2.6 }, { l: 'PPA', v: 2.1 }, { l: 'LMT', v: 1.9 }, { l: 'RTX', v: 2.8 }],
      rows: [['LMT', 'Lockheed', '+1.9'], ['RTX', 'RTX', '+2.8'], ['NOC', 'Northrop', '+2.2'], ['GD', 'Gen. Dyn.', '+1.5']],
      watch: ['Escalation Iran', 'Budget NATO', 'Nuovi ordini'] },
    staples: { tag: 'Consumer Staples', name: 'Beni di base', pct: '-0.3%', dir: 'down', why: "Il rifugio quando c'è paura. Difensivo.",
      bars: [{ l: 'XLP', v: -0.3 }, { l: 'VDC', v: -0.2 }, { l: 'PG', v: -0.1 }, { l: 'KO', v: -0.4 }, { l: 'COST', v: 0.3 }],
      rows: [['PG', 'P&G', '-0.1'], ['KO', 'Coca-Cola', '-0.4'], ['PEP', 'PepsiCo', '-0.2'], ['COST', 'Costco', '+0.3']],
      watch: ['CPI 14:30', 'Fiducia consumi', 'Risk-off'] },
    software: { tag: 'Tech Software', name: 'Software', pct: '+0.9%', dir: 'up', why: 'Vive di tassi e trimestrali.',
      bars: [{ l: 'IGV', v: 0.9 }, { l: 'WCLD', v: 1.2 }, { l: 'MSFT', v: 0.7 }, { l: 'CRM', v: 1.4 }, { l: 'NOW', v: 1.1 }],
      rows: [['MSFT', 'Microsoft', '+0.7'], ['CRM', 'Salesforce', '+1.4'], ['ORCL', 'Oracle', '+0.6'], ['NOW', 'ServiceNow', '+1.1']],
      watch: ['Yield 10Y', 'Earnings', 'Guidance cloud'] },
    chip: { tag: 'Semis & Hardware', name: 'Chip', pct: '-1.1%', dir: 'down', why: 'Ciclo tech + geopolitica. La Cina muove tutto.',
      bars: [{ l: 'SMH', v: -1.1 }, { l: 'SOXX', v: -1.3 }, { l: 'NVDA', v: -1.8 }, { l: 'AMD', v: -0.9 }, { l: 'TSM', v: -0.7 }],
      rows: [['NVDA', 'Nvidia', '-1.8'], ['AMD', 'AMD', '-0.9'], ['TSM', 'TSMC', '-0.7'], ['AVGO', 'Broadcom', '-1.2']],
      watch: ['Export Cina', 'Domanda AI', 'Prezzi memoria'] },
    industria: { tag: 'Infrastructure & Industrials', name: 'Industria', pct: '+0.5%', dir: 'up', why: 'Economia reale: PMI, spesa pubblica, energia.',
      bars: [{ l: 'XLI', v: 0.5 }, { l: 'PAVE', v: 0.8 }, { l: 'IFRA', v: 0.6 }, { l: 'CAT', v: 0.9 }, { l: 'DE', v: 0.4 }],
      rows: [['CAT', 'Caterpillar', '+0.9'], ['DE', 'Deere', '+0.4'], ['HON', 'Honeywell', '+0.5'], ['GE', 'GE Aero', '+1.1']],
      watch: ['PMI manifattura', 'Infrastrutture', 'Petrolio'] }
  };
  var RAMP = ['#7a5f14', '#b48a12', '#e2ac12', '#f5c518', '#ffd60a']; // integrated yellow scale

  var overlay = document.getElementById('overlay');
  var wrap = document.getElementById('sheetWrap');
  var body = document.getElementById('sheetBody');
  var sheet = document.getElementById('sheet');

  function buildChart(bars) {
    var max = Math.max.apply(null, bars.map(function (b) { return Math.abs(b.v); }));
    var root = document.createElement('div');
    root.className = 'bars';
    bars.forEach(function (b) {
      var r = Math.abs(b.v) / max;
      var h = Math.max(8, r * 100);
      var c = RAMP[Math.round(r * (RAMP.length - 1))];
      var bw = document.createElement('div');
      bw.className = 'barwrap';
      var val = document.createElement('span');
      val.className = 'bv ' + (b.v >= 0 ? 'p' : 'n');
      val.textContent = (b.v >= 0 ? '+' : '') + b.v + '%';
      var bar = document.createElement('div');
      bar.className = 'bar';
      bar.style.height = h + '%';
      bar.style.background = 'linear-gradient(180deg,' + c + ',' + c + 'cc)';
      bar.style.boxShadow = '0 0 ' + (6 + r * 22) + 'px rgba(255,214,10,' + (0.12 + r * 0.4).toFixed(2) + ')';
      var lab = document.createElement('span');
      lab.className = 'bl';
      lab.textContent = b.l;
      bw.appendChild(val); bw.appendChild(bar); bw.appendChild(lab);
      root.appendChild(bw);
    });
    return root;
  }

  function buildTable(rows) {
    var table = document.createElement('table');
    table.className = 'tbl';
    var thead = document.createElement('thead');
    var htr = document.createElement('tr');
    ['Titolo', '', 'Var'].forEach(function (label, idx) {
      var th = document.createElement('th');
      if (idx === 2) th.className = 'r';
      th.textContent = label;
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    var tbody = document.createElement('tbody');
    rows.forEach(function (r) {
      var tr = document.createElement('tr');
      var tk = document.createElement('td'); tk.className = 'tk'; tk.textContent = r[0];
      var nm = document.createElement('td'); nm.className = 'nm'; nm.textContent = r[1];
      var vr = document.createElement('td'); vr.className = (r[2].indexOf('-') === 0 ? 'n' : 'p'); vr.textContent = r[2] + '%';
      tr.appendChild(tk); tr.appendChild(nm); tr.appendChild(vr);
      tbody.appendChild(tr);
    });
    table.appendChild(thead); table.appendChild(tbody);
    return table;
  }

  function openModal(card) {
    var d = DETAILS[card.dataset.key];
    if (!d) return;
    body.textContent = '';

    var tag = document.createElement('div');
    tag.className = 's-tag';
    tag.textContent = d.tag;

    var title = document.createElement('div');
    title.className = 's-title';
    var h3 = document.createElement('h3'); h3.textContent = d.name;
    var pct = document.createElement('span'); pct.className = 's-pct ' + d.dir; pct.textContent = d.pct;
    title.appendChild(h3); title.appendChild(pct);

    var why = document.createElement('p');
    why.className = 's-why';
    why.textContent = d.why;

    var grid = document.createElement('div');
    grid.className = 's-grid';
    var p1 = document.createElement('div'); p1.className = 'panel';
    var p1h = document.createElement('h5'); p1h.textContent = 'Andamento oggi';
    p1.appendChild(p1h); p1.appendChild(buildChart(d.bars));
    var p2 = document.createElement('div'); p2.className = 'panel';
    var p2h = document.createElement('h5'); p2h.textContent = 'Titoli chiave';
    p2.appendChild(p2h); p2.appendChild(buildTable(d.rows));
    grid.appendChild(p1); grid.appendChild(p2);

    var watch = document.createElement('div');
    watch.className = 'watch';
    d.watch.forEach(function (w, i) {
      var chip = document.createElement('span');
      chip.className = 'chip' + (i === 0 ? ' alert' : '');
      chip.textContent = w;
      watch.appendChild(chip);
    });

    body.appendChild(tag);
    body.appendChild(title);
    body.appendChild(why);
    body.appendChild(grid);
    body.appendChild(watch);

    var r = card.getBoundingClientRect();
    wrap.style.setProperty('--ox', (r.left + r.width / 2) + 'px');
    wrap.style.setProperty('--oy', (r.top + r.height / 2) + 'px');
    overlay.style.display = 'block';
    void overlay.offsetWidth; // force reflow so the zoom transition runs
    overlay.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closeModal() {
    overlay.classList.remove('open');
    document.body.style.overflow = '';
    setTimeout(function () {
      if (!overlay.classList.contains('open')) overlay.style.display = 'none';
    }, 560);
  }
  cards.forEach(function (c) { c.addEventListener('click', function () { if (!moved) openModal(c); }); });
  var closeBtn = document.getElementById('close');
  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  if (wrap) wrap.addEventListener('click', function (e) { if (e.target === wrap) closeModal(); });
  if (sheet) sheet.addEventListener('click', function (e) { e.stopPropagation(); });
  window.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });

  /* blob parallax */
  var blobs = [].slice.call(document.querySelectorAll('.blob'));
  window.addEventListener('pointermove', function (e) {
    var x = e.clientX / window.innerWidth - 0.5, y = e.clientY / window.innerHeight - 0.5;
    blobs.forEach(function (b, i) {
      var k = (i + 1) * 16;
      b.style.marginLeft = (x * k) + 'px';
      b.style.marginTop = (y * k) + 'px';
    });
  });
})();
