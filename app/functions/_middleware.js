/**
 * ForwardGuidex dashboard — password gate (Cloudflare Pages Function).
 *
 * Runs before every request to the Pages project. Requires HTTP Basic Auth;
 * the password is read from the DASHBOARD_PASSWORD environment variable set on
 * the Pages project (Settings -> Variables and Secrets -> Production).
 *
 * Fails CLOSED: if DASHBOARD_PASSWORD is unset, every request is denied (503) —
 * the dashboard is never served publicly. Any username is accepted; only the
 * password is checked (constant-time comparison).
 */
function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return out === 0;
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
  const header = request.headers.get("Authorization") || "";
  const [scheme, encoded] = header.split(" ");
  if (scheme === "Basic" && encoded) {
    let decoded = "";
    try {
      decoded = atob(encoded);
    } catch (_e) {
      decoded = "";
    }
    const sep = decoded.indexOf(":");
    const pass = sep >= 0 ? decoded.slice(sep + 1) : "";
    if (timingSafeEqual(pass, expected)) {
      return next();
    }
  }
  return new Response("Authentication required.", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="ForwardGuidex", charset="UTF-8"',
      "Content-Type": "text/plain; charset=utf-8",
    },
  });
}
