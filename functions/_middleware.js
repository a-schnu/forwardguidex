/**
 * ForwardGuidex dashboard — access gate (Cloudflare Pages Function).
 *
 * LOCATION MATTERS: this file MUST live in the repo-root `functions/` directory,
 * NOT inside the deployed static root (`app/`). The daily/deploy workflows run
 * `wrangler pages deploy app` from the repo root, and wrangler compiles Functions
 * from `<cwd>/functions` — a `functions/` dir *inside* the assets directory is
 * ignored and served as a static file, which silently disables the gate and
 * publishes the dashboard. (See Cloudflare Pages docs: "the /functions directory
 * must be at the root of your Pages project, not in the static root such as
 * /dist".) The smoke tests assert an UNauthenticated request returns 401 so this
 * can never regress unnoticed.
 *
 * Runs before every request to the Pages project. The password comes from the
 * DASHBOARD_PASSWORD environment variable set on the Pages project
 * (Settings -> Variables and Secrets -> Production).
 *
 * TWO ways in, deliberately:
 *
 *   1. **HTTP Basic Auth** — what CI uses. `.github/workflows/smoke.py` and both
 *      workflows authenticate with an `Authorization: Basic` header, so this path
 *      must keep working exactly as before. Non-browser clients that fail auth
 *      still get `401` + the `WWW-Authenticate` challenge (smoke asserts the
 *      header is present).
 *   2. **A signed session cookie**, issued by the styled login form below. A
 *      browser cannot be shown a custom login screen while the server sends
 *      `WWW-Authenticate`, because the browser renders its own native credential
 *      dialog instead. So the challenge header is sent ONLY to clients that did
 *      not ask for HTML; a browser gets the same `401` status with the login page
 *      as the body. The status code never changes — no request returns 200
 *      without valid credentials.
 *
 * Fails CLOSED: if DASHBOARD_PASSWORD is unset, every request is denied (503) —
 * the dashboard is never served publicly. Any username is accepted for Basic;
 * only the password is checked, in constant time.
 */
const COOKIE_NAME = "fgx_session";
const SESSION_TTL_SEC = 12 * 60 * 60;
const LOGIN_PATH = "/__login";
const LOGOUT_PATH = "/__logout";
// Domain-separates the cookie-signing key from the password itself, so the
// cookie MAC can never be confused with (or used to probe) the credential.
const KEY_CONTEXT = "fgx-session-v1:";
const TOKEN_CONTEXT = "v1:";

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return out === 0;
}

function b64urlFromBytes(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function signingKey(password) {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(KEY_CONTEXT + password),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

async function mac(password, message) {
  const key = await signingKey(password);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return b64urlFromBytes(new Uint8Array(sig));
}

async function issueToken(password) {
  const exp = Math.floor(Date.now() / 1000) + SESSION_TTL_SEC;
  return `${exp}.${await mac(password, TOKEN_CONTEXT + exp)}`;
}

async function tokenIsValid(password, token) {
  if (typeof token !== "string" || token.length > 256) return false;
  const dot = token.indexOf(".");
  if (dot <= 0) return false;
  const exp = token.slice(0, dot);
  const sig = token.slice(dot + 1);
  // Reject anything that is not a plain integer before doing crypto work.
  if (!/^[0-9]{1,12}$/.test(exp)) return false;
  if (Number(exp) <= Math.floor(Date.now() / 1000)) return false;
  return timingSafeEqual(sig, await mac(password, TOKEN_CONTEXT + exp));
}

function readCookie(request, name) {
  const raw = request.headers.get("Cookie");
  if (!raw) return null;
  for (const part of raw.split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    if (part.slice(0, eq).trim() === name) return part.slice(eq + 1).trim();
  }
  return null;
}

function basicPassword(request) {
  const header = request.headers.get("Authorization") || "";
  const [scheme, encoded] = header.split(" ");
  if (scheme !== "Basic" || !encoded) return null;
  let decoded = "";
  try {
    decoded = atob(encoded);
  } catch (_e) {
    return null;
  }
  const sep = decoded.indexOf(":");
  return sep >= 0 ? decoded.slice(sep + 1) : "";
}

/** True when the client asked for HTML — i.e. a browser, not curl or CI. */
function wantsHtml(request) {
  return (request.headers.get("Accept") || "").includes("text/html");
}

const NO_STORE = {
  "Cache-Control": "no-store, no-cache, must-revalidate",
  // The gate response depends on all three: never let a shared cache reuse one
  // client's result for another.
  Vary: "Accept, Cookie, Authorization",
};

const LOGIN_CSS = `
*{box-sizing:border-box}
:root{
  --bg:#000; --white:#f4f4f7; --muted:#9696a2;
  --violet:#8b5cff; --violet-soft:#bda9ff;
  --glass-bg:rgba(255,255,255,.05); --glass-brd:rgba(255,255,255,.12);
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
}
html,body{height:100%;margin:0}
body{
  background:var(--bg); color:var(--white); font-family:var(--font);
  display:grid; place-items:center; padding:24px; overflow:hidden;
}
/* Two soft light sources, same violet/blue register as the dashboard. */
body::before,body::after{
  content:""; position:fixed; border-radius:50%; filter:blur(80px);
  pointer-events:none; z-index:0;
}
body::before{width:52vmax;height:52vmax;top:-18vmax;left:-12vmax;
  background:radial-gradient(circle,rgba(139,92,255,.34),transparent 68%)}
body::after{width:44vmax;height:44vmax;bottom:-16vmax;right:-10vmax;
  background:radial-gradient(circle,rgba(38,169,255,.26),transparent 68%)}
.card{
  position:relative; z-index:1; width:100%; max-width:392px; padding:38px 34px 32px;
  border:1px solid var(--glass-brd); border-radius:22px; background:var(--glass-bg);
  backdrop-filter:blur(22px) saturate(160%);
  box-shadow:0 24px 70px rgba(0,0,0,.7), inset 0 1px 0 rgba(255,255,255,.18);
  animation:rise .5s cubic-bezier(.2,.8,.2,1) both;
}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.brand{display:flex;align-items:center;gap:9px;margin-bottom:26px}
.brand svg{width:34px;height:auto;display:block}
.brand span{
  font-size:15px;font-weight:600;letter-spacing:.2px;
  background:linear-gradient(90deg,#fff,#bcd3ff);
  -webkit-background-clip:text;background-clip:text;color:transparent;
}
h1{margin:0 0 8px;font-size:29px;line-height:1.15;letter-spacing:-.6px;font-weight:650}
.caption{margin:0 0 26px;font-size:13.5px;line-height:1.5;color:var(--muted);max-width:31ch}
label{display:block;font-size:11px;letter-spacing:.9px;text-transform:uppercase;
  color:var(--muted);margin-bottom:8px}
input{
  width:100%;padding:13px 15px;font-size:15px;font-family:inherit;color:var(--white);
  background:rgba(255,255,255,.045);border:1px solid var(--glass-brd);border-radius:13px;
  outline:none;transition:border-color .18s,box-shadow .18s,background .18s;
}
input:focus{border-color:var(--violet);background:rgba(139,92,255,.07);
  box-shadow:0 0 0 3px rgba(139,92,255,.2),0 0 26px rgba(139,92,255,.28)}
button{
  width:100%;margin-top:16px;padding:13px 16px;font-size:14.5px;font-weight:600;
  font-family:inherit;color:#fff;cursor:pointer;border:0;border-radius:13px;
  background:linear-gradient(135deg,#8b5cff,#2f6fe0);
  box-shadow:0 8px 24px rgba(139,92,255,.34);
  transition:transform .14s,box-shadow .18s,filter .18s;
}
button:hover{filter:brightness(1.08);box-shadow:0 10px 30px rgba(139,92,255,.46)}
button:active{transform:translateY(1px)}
button:focus-visible,input:focus-visible{outline:2px solid var(--violet-soft);outline-offset:2px}
.err{
  display:flex;gap:8px;margin:0 0 18px;padding:10px 12px;font-size:12.5px;line-height:1.45;
  color:#ffcdbd;border:1px solid rgba(255,143,111,.34);border-radius:11px;
  background:rgba(255,143,111,.1);
}
.foot{margin:22px 0 0;font-size:11px;line-height:1.5;color:#6f6f7b}
@media (prefers-reduced-motion:reduce){.card{animation:none}button{transition:none}}
`;

// Same mark as the dashboard header (app/index.html), inlined: the login page is
// served BEFORE auth, so it must not reference any gated asset — no external
// stylesheet, font or image request.
const BRAND_MARK = `<svg viewBox="0 0 140 84" aria-hidden="true" focusable="false" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="hT" x1="70" y1="8" x2="120" y2="40" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#8fddff"/><stop offset=".5" stop-color="#26a9ff"/><stop offset="1" stop-color="#1f7cf0"/></linearGradient>
        <linearGradient id="hB" x1="78" y1="30" x2="122" y2="46" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#1e88f4"/><stop offset="1" stop-color="#1055d4"/></linearGradient>
        <linearGradient id="fd" x1="14" y1="0" x2="86" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#1f86ef" stop-opacity="0"/><stop offset=".55" stop-color="#2aa0ff" stop-opacity=".9"/><stop offset="1" stop-color="#3fb4ff" stop-opacity="1"/></linearGradient>
      </defs>
      <path d="M126 28 L72 6 L96 34 Z" fill="url(#hT)"/>
      <path d="M126 28 L96 34 L78 46 Z" fill="url(#hB)"/>
      <path d="M72 6 L126 28" fill="none" stroke="#c7ecff" stroke-width="1.3" stroke-opacity=".75" stroke-linecap="round"/>
      <path d="M80 33 Q48 39 16 52 Q46 44 78 39 Z" fill="url(#fd)"/>
      <path d="M74 42 Q49 50 24 63 Q49 54 71 47 Z" fill="url(#fd)" opacity=".82"/>
      <path d="M67 50 Q50 58 33 71 Q51 62 62 54 Z" fill="url(#fd)" opacity=".6"/>
    </svg>`;

function loginPage(status, { failed = false } = {}) {
  const error = failed
    ? '<p class="err" role="alert">Wrong password. Try again.</p>'
    : "";
  const html = `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>ForwardGuidex — Get inside</title>
<style>${LOGIN_CSS}</style>
</head><body>
<main class="card">
  <div class="brand">${BRAND_MARK}<span>ForwardGuidex</span></div>
  <h1>Get inside</h1>
  <p class="caption">Make your own predictions, and know what to expect from the future.</p>
  ${error}
  <form method="POST" action="${LOGIN_PATH}">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password"
           required autofocus spellcheck="false">
    <button type="submit">Enter</button>
  </form>
  <p class="foot">Private dashboard. Decision support only — not investment advice.</p>
</main>
</body></html>`;
  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", ...NO_STORE },
  });
}

function challenge() {
  return new Response("Authentication required.", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="ForwardGuidex", charset="UTF-8"',
      "Content-Type": "text/plain; charset=utf-8",
      ...NO_STORE,
    },
  });
}

function cookieHeader(value, maxAge) {
  return `${COOKIE_NAME}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`;
}

export async function onRequest(context) {
  const { request, env, next } = context;
  const expected = env.DASHBOARD_PASSWORD;
  if (!expected) {
    return new Response("Dashboard not configured (missing DASHBOARD_PASSWORD).", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }

  const url = new URL(request.url);

  if (url.pathname === LOGOUT_PATH) {
    return new Response(null, {
      status: 303,
      headers: { Location: "/", "Set-Cookie": cookieHeader("", 0), ...NO_STORE },
    });
  }

  if (url.pathname === LOGIN_PATH) {
    if (request.method !== "POST") {
      // Never 200 without credentials: the form is served as the 401 body.
      return loginPage(401);
    }
    // Defence in depth on top of SameSite=Strict: a cross-origin form post
    // cannot mint a session. `Origin` is absent on some clients, so only a
    // present-and-mismatched value is rejected.
    const origin = request.headers.get("Origin");
    if (origin && origin !== url.origin) {
      return new Response("Forbidden.", {
        status: 403,
        headers: { "Content-Type": "text/plain; charset=utf-8", ...NO_STORE },
      });
    }
    let submitted = "";
    try {
      submitted = String((await request.formData()).get("password") || "");
    } catch (_e) {
      submitted = "";
    }
    if (!timingSafeEqual(submitted, expected)) {
      return loginPage(401, { failed: true });
    }
    return new Response(null, {
      status: 303,
      headers: {
        Location: "/",
        "Set-Cookie": cookieHeader(await issueToken(expected), SESSION_TTL_SEC),
        ...NO_STORE,
      },
    });
  }

  const basic = basicPassword(request);
  if (basic !== null && timingSafeEqual(basic, expected)) {
    return next();
  }

  if (await tokenIsValid(expected, readCookie(request, COOKIE_NAME))) {
    return next();
  }

  // Same 401 either way; only the body and the challenge header differ. A
  // browser must NOT receive `WWW-Authenticate`, or it renders its own native
  // credential dialog instead of the page above.
  return wantsHtml(request) ? loginPage(401) : challenge();
}
