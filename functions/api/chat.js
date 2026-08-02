/**
 * ForwardGuidex — server-side AI chat proxy (Cloudflare Pages Function).
 *
 * SECURITY MODEL
 *   - The OpenRouter API key lives ONLY here, as a Cloudflare Pages environment
 *     secret (env.OPENROUTER_API_KEY). It is NEVER exposed to the browser: the
 *     dashboard's CSP is `connect-src 'self'`, so the widget can only POST to this
 *     same-origin endpoint, which then calls OpenRouter server-side.
 *   - This route sits BEHIND the site's HTTP Basic auth gate
 *     (functions/_middleware.js runs first), so only the authenticated owner can
 *     reach it — no extra auth needed here, and no public LLM proxy is exposed.
 *   - Fails CLOSED: if the key is unset, it returns 503 (never leaks, never guesses).
 *   - Input is capped (message count / length / context length) to bound cost/abuse.
 *
 * CONTRACT (see scratchpad CHAT_CONTRACT.md)
 *   POST /api/chat  { messages:[{role,content}], context:"<market summary>" }
 *   200 { reply:"<markdown>" }   |   non-2xx { error:"<message>" }
 *
 * Env: OPENROUTER_API_KEY (required), OPENROUTER_MODEL (optional, default below),
 *      OPENROUTER_WEB_SEARCH ("off" disables the OpenRouter web search).
 */

const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const DEFAULT_MODEL = "anthropic/claude-3.5-sonnet";

// Abuse / cost caps.
const MAX_MESSAGES = 24;
const MAX_MSG_CHARS = 4000;
const MAX_CONTEXT_CHARS = 4000;
const MAX_TOKENS = 900;

const SYSTEM_PROMPT =
  "Sei l'assistente AI di ForwardGuidex, una piattaforma personale di market intelligence.\n" +
  "Rispondi SOLO a domande di finanza, mercati, macroeconomia, banche centrali, tassi, valute, " +
  "materie prime, azioni, ETF, criptovalute, earnings e notizie economico-finanziarie.\n" +
  "Se la domanda NON riguarda questi temi, rifiuta in UNA frase gentile e riporta l'utente ai " +
  "mercati, senza rispondere nel merito.\n" +
  "NON dare consigli di investimento personalizzati, raccomandazioni di acquisto/vendita o target " +
  "di prezzo: offri fatti, contesto e spunti da verificare. Non sei un consulente finanziario abilitato.\n" +
  "Usa i DATI DEL CRUSCOTTO qui sotto quando pertinenti e cita i numeri reali. Quando usi la ricerca " +
  "web, cita le fonti con i link.\n" +
  "Rispondi in ITALIANO, in modo conciso e scannabile: usa **grassetti** ed elenchi puntati. " +
  "Niente tabelle o HTML.";

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function sanitizeMessages(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const m of raw.slice(-MAX_MESSAGES)) {
    if (!m || typeof m.content !== "string") continue;
    const role = m.role === "assistant" ? "assistant" : "user";
    const content = m.content.slice(0, MAX_MSG_CHARS);
    if (content.trim()) out.push({ role: role, content: content });
  }
  return out;
}

export async function onRequest(context) {
  const { request, env } = context;

  if (request.method !== "POST") {
    return new Response(JSON.stringify({ error: "Metodo non consentito." }), {
      status: 405,
      headers: { "content-type": "application/json; charset=utf-8", allow: "POST" },
    });
  }

  // Defense-in-depth CSRF guard: when the browser sends an Origin header it MUST
  // match this deployment's own origin. Same-origin requests from our page carry
  // our origin (or none); a cross-site POST that tried to burn OpenRouter credit
  // via the owner's cached Basic-auth session would carry a foreign Origin -> 403.
  // (The application/json content-type already forces a CORS preflight that fails
  // cross-origin; this is a second, explicit layer.)
  const origin = request.headers.get("Origin");
  if (origin && origin !== new URL(request.url).origin) {
    return json({ error: "Origine non consentita." }, 403);
  }

  const key = env.OPENROUTER_API_KEY;
  if (!key) {
    return json({ error: "Assistente non configurato (OPENROUTER_API_KEY mancante su Cloudflare Pages)." }, 503);
  }

  let body;
  try {
    body = await request.json();
  } catch (_e) {
    return json({ error: "Richiesta non valida." }, 400);
  }

  const messages = sanitizeMessages(body && body.messages);
  if (!messages.length || messages[messages.length - 1].role !== "user") {
    return json({ error: "Nessun messaggio utente." }, 400);
  }
  const marketContext =
    body && typeof body.context === "string" ? body.context.slice(0, MAX_CONTEXT_CHARS) : "";

  const model = env.OPENROUTER_MODEL || DEFAULT_MODEL;
  // OpenRouter enables web search via the ":online" model suffix; opt out with
  // OPENROUTER_WEB_SEARCH=off.
  const webOff = String(env.OPENROUTER_WEB_SEARCH || "").toLowerCase() === "off";
  const modelSlug = webOff || model.indexOf(":online") !== -1 ? model : model + ":online";

  const system =
    SYSTEM_PROMPT +
    (marketContext ? "\n\n=== DATI DEL CRUSCOTTO (usali quando pertinenti) ===\n" + marketContext : "");

  const payload = {
    model: modelSlug,
    messages: [{ role: "system", content: system }].concat(messages),
    max_tokens: MAX_TOKENS,
    temperature: 0.3,
  };

  let resp;
  try {
    resp = await fetch(OPENROUTER_URL, {
      method: "POST",
      headers: {
        Authorization: "Bearer " + key,
        "content-type": "application/json",
        // OpenRouter attribution headers (recommended).
        "HTTP-Referer": new URL(request.url).origin,
        "X-Title": "ForwardGuidex",
      },
      body: JSON.stringify(payload),
    });
  } catch (_e) {
    return json({ error: "Assistente irraggiungibile. Riprova." }, 502);
  }

  if (!resp.ok) {
    let detail = "";
    try {
      const err = await resp.json();
      detail = (err && err.error && err.error.message) || "";
    } catch (_e) {
      /* ignore */
    }
    if (resp.status === 429) {
      return json({ error: "Assistente occupato (limite richieste). Riprova tra poco." }, 429);
    }
    return json({ error: "Errore dell'assistente" + (detail ? ": " + detail.slice(0, 160) : ".") }, 502);
  }

  let data;
  try {
    data = await resp.json();
  } catch (_e) {
    return json({ error: "Risposta non valida dall'assistente." }, 502);
  }
  const msg = data && data.choices && data.choices[0] && data.choices[0].message;
  const reply = msg ? String(msg.content || "").trim() : "";
  if (!reply) {
    return json({ error: "Nessuna risposta generata." }, 502);
  }
  return json({ reply: reply });
}
