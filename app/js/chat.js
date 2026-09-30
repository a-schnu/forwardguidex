/* ForwardGuidex — floating AI chat widget.
 *
 * Moved out of app.js unchanged in behaviour (streaming, stop, web toggle,
 * retry, starter chips). New: `askAssistant()` lets any view hand the chat a
 * drafted question, and `setViewContext()` tells the grounding context what
 * the user is looking at (the open detail panel, not just the active tab).
 *
 * Security: user/system turns are textContent only; assistant turns are
 * marked + DOMPurify with the same allow-list as the Morning Brief.
 */

import { el, fmtPct, fmtRate, fmtDate, setSafeExternalLink } from './util.js';

let currentSnapshot = null;
let viewContext = () => '';

/** Called once the snapshot is verified and parsed. */
export function setChatSnapshot(snapshot) {
  currentSnapshot = snapshot;
}

/** `fn()` returns a short Italian sentence describing the current view. */
export function setViewContext(fn) {
  if (typeof fn === 'function') viewContext = fn;
}

/* Same sanitiser config as renderBrief — the ONLY difference is these are
 * assistant chat replies rather than the Morning Brief. */
const CHAT_SANITIZE = {
  ALLOWED_TAGS: ['h1', 'h2', 'h3', 'p', 'ul', 'ol', 'li', 'strong', 'em', 'blockquote', 'code', 'pre', 'a', 'br'],
  ALLOWED_ATTR: ['href', 'title'],
  ALLOW_DATA_ATTR: false,
  ALLOW_ARIA_ATTR: false
};

// In-memory conversation (user + assistant turns), trimmed to the last ~12.
const chatHistory = [];
const CHAT_MAX_TURNS = 12;
const CHAT_MAX_INPUT = 2000;
let chatBusy = false;
/* Live request, so the user can stop a generation instead of waiting it out. */
let chatAbort = null;
/* 'auto' lets the server decide from the question; 'on'/'off' force it. */
let chatWebMode = 'auto';
const CHAT_WEB_MODES = ['auto', 'on', 'off'];
const CHAT_WEB_LABEL = { auto: 'Web: auto', on: 'Web: sempre', off: 'Web: mai' };

const CHAT_GREETING =
  'Ciao! Sono l’**Assistente AI** di ForwardGuidex. ' +
  'Come posso esserti utile oggi su mercati, indici o tassi?';

/** Trim the running history to the last CHAT_MAX_TURNS entries. */
function trimChatHistory() {
  if (chatHistory.length > CHAT_MAX_TURNS) {
    chatHistory.splice(0, chatHistory.length - CHAT_MAX_TURNS);
  }
}

/**
 * Reset the conversation to a fresh state: clear the in-memory history AND the
 * visible log, then show the greeting plus data-derived starter questions.
 */
function resetChat() {
  if (chatBusy) return;
  chatHistory.length = 0;
  const log = document.getElementById('chatLog');
  if (log) log.textContent = '';
  appendChatMessage('assistant', CHAT_GREETING);
  renderChatSuggestions();
}

/* ---------- grounding context ---------- */

/**
 * Compact plain-text market summary the client sends as grounding context.
 * Every field is guarded — arrays may be empty or missing.
 *
 * Treated as UNTRUSTED by the server (see functions/api/chat.js): parts of it —
 * news headlines, filing titles — are third-party text, so it is delivered
 * inside a nonce-delimited data block rather than as instructions.
 */
function buildMarketContext(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return '';
  const parts = [];
  const meta = snapshot.meta || {};
  if (meta.data_as_of) parts.push('Dati al ' + fmtDate(meta.data_as_of) + '.');

  /* Data health first: an assistant that quotes a stale or partial snapshot
   * without saying so is worse than one that admits the gap. */
  const health = [];
  if (meta.freshness && meta.freshness !== 'FRESH') health.push('freschezza=' + meta.freshness);
  if (meta.quality && meta.quality !== 'OK') health.push('qualità=' + meta.quality);
  if (meta.market_state_at_generation) health.push('fase di mercato=' + meta.market_state_at_generation);
  if (health.length) {
    parts.push('ATTENZIONE stato dei dati: ' + health.join(', ') +
      ' — segnalalo all\'utente prima di commentare i numeri.');
  }

  /* What the user is actually looking at right now. */
  let seen = '';
  try { seen = viewContext() || ''; } catch (_e) { seen = ''; }
  if (seen) parts.push(seen);

  const indices = Array.isArray(snapshot.indices) ? snapshot.indices : [];
  if (indices.length) {
    parts.push('Indici (1g): ' + indices.slice(0, 10)
      .map((i) => (i.name || i.ticker || '?') + ' ' + fmtPct(i.ret_1d)).join(', ') + '.');
  }

  const sectors = (Array.isArray(snapshot.sectors) ? snapshot.sectors : [])
    .filter((s) => Number.isFinite(s.avg_ret_1d))
    .slice().sort((a, b) => b.avg_ret_1d - a.avg_ret_1d);
  if (sectors.length) {
    parts.push('Settori (1g): ' + sectors.slice(0, 8)
      .map((s) => (s.label || s.key || '?') + ' ' + fmtPct(s.avg_ret_1d)).join(', ') + '.');
  }

  const rates = Array.isArray(snapshot.rates) ? snapshot.rates : [];
  if (rates.length) {
    parts.push('Tassi: ' + rates.slice(0, 8)
      .map((r) => (r.name || r.series_id || '?') + ' ' + fmtRate(r.value)).join(', ') + '.');
  }

  const crypto = Array.isArray(snapshot.crypto) ? snapshot.crypto : [];
  if (crypto.length) {
    parts.push('Crypto (1g): ' + crypto.slice(0, 5)
      .map((c) => (c.name || c.ticker || '?') + ' ' + fmtPct(c.ret_1d)).join(', ') + '.');
  }

  const cb = Array.isArray(snapshot.cb_events) ? snapshot.cb_events : [];
  if (cb.length) {
    const DIR = { hike: 'rialzo', cut: 'taglio', hold: 'invariato' };
    parts.push('Banche centrali: ' + cb.slice(0, 6)
      .map((e) => (e.bank || '?') + ' ' + fmtRate(e.rate) + ' (' + (DIR[e.direction] || e.direction || '—') + ')')
      .join(', ') + '.');
  }

  const movers = snapshot.movers || {};
  const gainers = Array.isArray(movers.gainers) ? movers.gainers : [];
  const losers = Array.isArray(movers.losers) ? movers.losers : [];
  if (gainers.length) {
    parts.push('Top rialzi: ' + gainers.slice(0, 5)
      .map((m) => (m.name || m.ticker || '?') + ' ' + fmtPct(m.ret_1d)).join(', ') + '.');
  }
  if (losers.length) {
    parts.push('Top ribassi: ' + losers.slice(0, 5)
      .map((m) => (m.name || m.ticker || '?') + ' ' + fmtPct(m.ret_1d)).join(', ') + '.');
  }

  const earnings = Array.isArray(snapshot.earnings) ? snapshot.earnings : [];
  if (earnings.length) {
    parts.push('Earnings in arrivo: ' + earnings.slice(0, 6)
      .map((e) => (e.name || e.ticker || '?') + ' (' + fmtDate(e.date) + ')').join(', ') + '.');
  }

  const triggers = (Array.isArray(snapshot.triggers) ? snapshot.triggers : [])
    .map((t) => t && t.title).filter(Boolean);
  if (triggers.length) {
    parts.push('Catalizzatori: ' + triggers.slice(0, 5).join('; ') + '.');
  }

  /* Headlines the dashboard is showing, so "di cosa parla questa notizia?"
   * works without a web search. Third-party text — see the note above. */
  const news = (Array.isArray(snapshot.headlines) ? snapshot.headlines : [])
    .map((n) => n && n.title && ((n.topic ? '[' + n.topic + '] ' : '') + n.title))
    .filter(Boolean);
  if (news.length) {
    parts.push('Titoli in home: ' + news.slice(0, 8).join(' | ') + '.');
  }

  let ctx = parts.join('\n');
  if (ctx.length > 4500) ctx = ctx.slice(0, 4499) + '…';
  return ctx;
}

/**
 * Starter questions built from the snapshot actually loaded, so they name real
 * movers and real events instead of being decorative placeholders.
 */
function buildChatSuggestions(snapshot) {
  const out = [];
  if (!snapshot || typeof snapshot !== 'object') return out;

  const movers = snapshot.movers || {};
  const losers = Array.isArray(movers.losers) ? movers.losers : [];
  const gainers = Array.isArray(movers.gainers) ? movers.gainers : [];
  const worst = losers[0] && (losers[0].name || losers[0].ticker);
  const best = gainers[0] && (gainers[0].name || gainers[0].ticker);
  if (worst) out.push('Perché ' + worst + ' scende oggi?');
  if (best && out.length < 2) out.push('Cosa spinge ' + best + '?');

  const cb = Array.isArray(snapshot.cb_events) ? snapshot.cb_events : [];
  if (cb.length && cb[0].bank) out.push('Cosa implica la mossa della ' + cb[0].bank + '?');

  const earnings = Array.isArray(snapshot.earnings) ? snapshot.earnings : [];
  const next = earnings[0] && (earnings[0].name || earnings[0].ticker);
  if (next) out.push('Cosa aspettarsi dagli earnings di ' + next + '?');

  const sectors = (Array.isArray(snapshot.sectors) ? snapshot.sectors : [])
    .filter((s) => Number.isFinite(s.avg_ret_1d));
  if (sectors.length > 1) out.push('Che rotazione settoriale vedi oggi?');

  if (!out.length) out.push('Riassumi la giornata sui mercati');
  return out.slice(0, 4);
}

/** Render the starter questions as one-click chips under the greeting. */
function renderChatSuggestions() {
  const log = document.getElementById('chatLog');
  if (!log) return;
  const items = buildChatSuggestions(currentSnapshot);
  if (!items.length) return;
  const wrap = el('div', 'chat-chips');
  items.forEach((q) => {
    const chip = el('button', 'chat-chip', q);
    chip.type = 'button';
    chip.addEventListener('click', () => {
      if (chatBusy) return;
      if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      sendChat(q);
    });
    wrap.appendChild(chip);
  });
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
}

/* ---------- rendering ---------- */

/** Sanitise markdown into a bubble. innerHTML only ever receives clean output. */
function renderChatMarkdown(bubble, md) {
  const clean = window.DOMPurify.sanitize(window.marked.parse(md || ''), CHAT_SANITIZE);
  bubble.innerHTML = clean; // sanitised output only
  bubble.querySelectorAll('a').forEach(setSafeExternalLink);
}

/**
 * Append a message bubble to #chatLog and auto-scroll.
 *   - role 'user'   -> textContent only (never HTML).
 *   - role 'system' -> textContent only, error/system styling.
 *   - role 'assistant' -> markdown via the SAME marked+DOMPurify pattern as
 *     renderBrief; links hardened with setSafeExternalLink.
 */
function appendChatMessage(role, content) {
  const log = document.getElementById('chatLog');
  if (!log) return null;
  const msg = el('div', 'chat-msg chat-' + role);

  if (role === 'assistant') {
    const bubble = el('div', 'chat-bubble brief-body');
    renderChatMarkdown(bubble, typeof content === 'string' ? content : '');
    msg.appendChild(bubble);
  } else {
    // user + system: plain text, never parsed as HTML
    msg.appendChild(el('div', 'chat-bubble', typeof content === 'string' ? content : String(content)));
  }

  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
  return msg;
}

/** Insert the "sto scrivendo…" typing indicator; returns the node to remove. */
function showChatTyping() {
  const log = document.getElementById('chatLog');
  if (!log) return null;
  const msg = el('div', 'chat-msg chat-assistant');
  const bubble = el('div', 'chat-bubble chat-typing');
  bubble.setAttribute('aria-label', 'Sto scrivendo…');
  for (let i = 0; i < 3; i++) bubble.appendChild(el('span', 'chat-dot'));
  msg.appendChild(bubble);
  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
  return msg;
}

/** A "copy" affordance plus, when the answer used web search, its sources. */
function decorateAssistantMessage(msg, text, info) {
  if (!msg) return;
  const foot = el('div', 'chat-foot');

  const copy = el('button', 'chat-mini', 'Copia');
  copy.type = 'button';
  copy.addEventListener('click', () => {
    const done = () => { copy.textContent = 'Copiato'; setTimeout(() => { copy.textContent = 'Copia'; }, 1400); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, () => { copy.textContent = 'Errore'; });
    }
  });
  foot.appendChild(copy);

  if (info && info.web) foot.appendChild(el('span', 'chat-badge', 'cercato sul web'));
  if (info && info.truncated) foot.appendChild(el('span', 'chat-badge warn', 'risposta troncata'));
  msg.appendChild(foot);

  const cites = (info && Array.isArray(info.citations) ? info.citations : []).slice(0, 5);
  if (cites.length) {
    const list = el('div', 'chat-cites');
    cites.forEach((c, i) => {
      const a = el('a', 'chat-cite', String(i + 1) + '. ' + (c.title || c.url));
      a.setAttribute('href', c.url);
      setSafeExternalLink(a);
      list.appendChild(a);
    });
    msg.appendChild(list);
  }
}

/** Offer a one-click retry instead of leaving the user at a dead end. */
function appendChatRetry(text) {
  const log = document.getElementById('chatLog');
  if (!log) return;
  const wrap = el('div', 'chat-chips');
  const chip = el('button', 'chat-chip', 'Riprova');
  chip.type = 'button';
  chip.addEventListener('click', () => {
    if (chatBusy) return;
    if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
    sendChat(text);
  });
  wrap.appendChild(chip);
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
}

/** Grow the textarea with its content up to the CSS max-height. */
function autoGrowChatInput(ta) {
  if (!ta) return;
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';
}

/* ---------- send ---------- */

function setChatBusy(busy) {
  chatBusy = busy;
  const input = document.getElementById('chatInput');
  const sendBtn = document.getElementById('chatSend');
  if (input) input.disabled = false; // stay typable: the user can queue a thought
  if (sendBtn) {
    sendBtn.classList.toggle('is-stop', busy);
    sendBtn.setAttribute('aria-label', busy ? 'Interrompi generazione' : 'Invia messaggio');
    sendBtn.title = busy ? 'Interrompi' : 'Invia';
  }
}

/** Abort an in-flight generation, keeping whatever text already arrived. */
function stopChat() {
  if (chatAbort) chatAbort.abort();
}

/**
 * Send a turn and stream the reply.
 *
 * Streaming is the whole point of the response handling here: the previous
 * version waited for the complete answer, so a slow free-tier model looked
 * indistinguishable from a broken one. Tokens are rendered as they arrive and
 * the send button becomes a stop button.
 */
function sendChat(preset) {
  const input = document.getElementById('chatInput');
  if (!input) return;
  if (chatBusy) { stopChat(); return; }

  let text = typeof preset === 'string' ? preset : (input.value || '').trim();
  if (!text) return;
  if (text.length > CHAT_MAX_INPUT) text = text.slice(0, CHAT_MAX_INPUT);

  appendChatMessage('user', text);
  chatHistory.push({ role: 'user', content: text });
  trimChatHistory();

  if (typeof preset !== 'string') {
    input.value = '';
    autoGrowChatInput(input);
  }

  setChatBusy(true);
  let typing = showChatTyping();
  chatAbort = new AbortController();

  /* Bubble the deltas land in, created on the first token so the typing dots
   * stay visible until the model actually starts speaking. */
  let msg = null;
  let bubble = null;
  let acc = '';
  let pending = false;
  let info = { web: false, citations: [], truncated: false };

  const flush = () => {
    pending = false;
    if (bubble) {
      renderChatMarkdown(bubble, acc);
      const log = document.getElementById('chatLog');
      if (log) log.scrollTop = log.scrollHeight;
    }
  };
  /* Re-parsing markdown on every token is wasteful and flickers; coalesce into
   * one render per animation frame. */
  const schedule = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(flush);
  };

  const onDelta = (chunk) => {
    if (!msg) {
      if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
      typing = null;
      msg = el('div', 'chat-msg chat-assistant');
      bubble = el('div', 'chat-bubble brief-body chat-streaming');
      msg.appendChild(bubble);
      const log = document.getElementById('chatLog');
      if (log) log.appendChild(msg);
    }
    acc += chunk;
    schedule();
  };

  const finish = (errorText) => {
    if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
    typing = null;
    flush();
    if (bubble) bubble.classList.remove('chat-streaming');

    if (acc.trim()) {
      chatHistory.push({ role: 'assistant', content: acc });
      trimChatHistory();
      decorateAssistantMessage(msg, acc, info);
    } else {
      // Drop the unanswered user turn so a retry doesn't send two consecutive
      // user messages (which would break every follow-up).
      if (chatHistory.length && chatHistory[chatHistory.length - 1].role === 'user') chatHistory.pop();
    }
    if (errorText) {
      appendChatMessage('system', errorText);
      appendChatRetry(text);
    }
    chatAbort = null;
    setChatBusy(false);
    const el2 = document.getElementById('chatInput');
    if (el2) el2.focus();
  };

  fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    signal: chatAbort.signal,
    body: JSON.stringify({
      messages: chatHistory,
      context: buildMarketContext(currentSnapshot),
      web: chatWebMode,
      stream: true
    })
  })
    .then((res) => {
      const ctype = res.headers.get('content-type') || '';
      if (!res.ok || ctype.indexOf('text/event-stream') === -1) {
        // Failures happen before the first byte, so they still carry a status
        // and a JSON body.
        return res.json().catch(() => ({})).then((data) => {
          finish((data && typeof data.error === 'string' && data.error) ||
            'Si è verificato un errore. Riprova.');
        });
      }
      return readChatStream(res, onDelta, info).then((err) => finish(err));
    })
    .catch((e) => {
      if (e && e.name === 'AbortError') { finish(null); return; }
      finish("Impossibile contattare l'assistente. Controlla la connessione e riprova.");
    });
}

/**
 * Consume our SSE wire format: `data: {meta|delta|done|error}`.
 * Resolves with an error string, or null when the stream ended cleanly.
 */
function readChatStream(res, onDelta, info) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let error = null;

  const pump = () => reader.read().then(({ done, value }) => {
    if (done) return error;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, cut).trim();
      buffer = buffer.slice(cut + 2);
      if (!frame.startsWith('data:')) continue;
      let obj;
      try { obj = JSON.parse(frame.slice(5).trim()); } catch (_e) { continue; }
      if (obj.meta) { info.web = !!obj.meta.web; continue; }
      if (typeof obj.delta === 'string') { onDelta(obj.delta); continue; }
      if (obj.done) {
        info.truncated = !!obj.truncated;
        if (Array.isArray(obj.citations)) info.citations = obj.citations;
        continue;
      }
      if (typeof obj.error === 'string') error = obj.error;
    }
    return pump();
  });

  return pump();
}

/* ---------- wiring ---------- */

/** Cycle auto -> on -> off and reflect it on the button. */
function cycleChatWeb(btn) {
  const next = CHAT_WEB_MODES[(CHAT_WEB_MODES.indexOf(chatWebMode) + 1) % CHAT_WEB_MODES.length];
  chatWebMode = next;
  btn.setAttribute('data-mode', next);
  btn.title = CHAT_WEB_LABEL[next];
  btn.setAttribute('aria-label', CHAT_WEB_LABEL[next]);
}

let openChatPanel = null;

/**
 * Open the chat with a drafted question in the input, focused and NOT sent:
 * the user reviews it first (a detail panel should never spend a model call
 * on its own).
 */
export function askAssistant(question) {
  if (!openChatPanel) return;
  openChatPanel();
  const input = document.getElementById('chatInput');
  if (input && typeof question === 'string') {
    input.value = question.slice(0, CHAT_MAX_INPUT);
    autoGrowChatInput(input);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

/** Wire the fab/panel toggle, close, Esc, Enter-to-send. Called once from main(). */
export function wireChat() {
  const fab = document.getElementById('chatFab');
  const panel = document.getElementById('chatPanel');
  const closeBtn = document.getElementById('chatClose');
  const newBtn = document.getElementById('chatNew');
  const webBtn = document.getElementById('chatWeb');
  const input = document.getElementById('chatInput');
  const sendBtn = document.getElementById('chatSend');
  if (!fab || !panel) return;

  const openChat = () => {
    panel.hidden = false;
    fab.classList.add('open');
    fab.setAttribute('aria-expanded', 'true');
    // Show the greeting the first time the panel is opened in this session.
    const log = document.getElementById('chatLog');
    if (log && !log.childNodes.length) {
      appendChatMessage('assistant', CHAT_GREETING);
      renderChatSuggestions();
    }
    if (input) { autoGrowChatInput(input); input.focus(); }
  };
  openChatPanel = openChat;
  const closeChat = (returnFocus) => {
    panel.hidden = true;
    fab.classList.remove('open');
    fab.setAttribute('aria-expanded', 'false');
    if (returnFocus) fab.focus();
  };

  fab.addEventListener('click', () => { if (panel.hidden) openChat(); else closeChat(false); });
  if (closeBtn) closeBtn.addEventListener('click', () => closeChat(true));
  if (newBtn) newBtn.addEventListener('click', () => { resetChat(); if (input) input.focus(); });
  if (webBtn) {
    webBtn.setAttribute('data-mode', chatWebMode);
    webBtn.title = CHAT_WEB_LABEL[chatWebMode];
    webBtn.addEventListener('click', () => cycleChatWeb(webBtn));
  }
  if (sendBtn) sendBtn.addEventListener('click', () => sendChat());

  if (input) {
    input.addEventListener('input', () => autoGrowChatInput(input));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
    });
  }

  // Esc stops a generation if one is running, otherwise closes the panel —
  // unless a detail panel or the search palette is on top: that layer owns Esc.
  window.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || panel.hidden) return;
    if (document.body.classList.contains('layer-open')) return;
    if (chatBusy) { stopChat(); return; }
    closeChat(true);
  });
}
