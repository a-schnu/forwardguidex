/**
 * Behavioural tests for the dashboard access gate (`functions/_middleware.js`).
 *
 * Run with `node --test tests/gate/` — no dependencies, no dev server. Cloudflare
 * Pages Functions run on workerd, whose request/response/crypto primitives are
 * the same web-standard APIs Node exposes globally, so the gate's logic can be
 * exercised directly.
 *
 * These assertions are the contract `.github/workflows/smoke.py` relies on
 * against the real deployment. The point of having them here too is that a
 * mistake in the gate is caught before anything is uploaded to Cloudflare —
 * smoke only runs after the bytes are live.
 *
 * The middleware is loaded through a temporary `.mjs` copy: it uses ESM syntax
 * in a `.js` file, which is exactly what Pages expects, but Node would treat as
 * CommonJS. Copying avoids adding a root `package.json` just for the tests.
 */
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, it } from "node:test";
import { pathToFileURL } from "node:url";

const PASSWORD = "correct-horse-battery-staple";
const ORIGIN = "https://fgx-dashboard.pages.dev";
const SERVED = "DASHBOARD-HTML";

let onRequest;

before(async () => {
  const src = await readFile(new URL("../../functions/_middleware.js", import.meta.url), "utf8");
  const dir = await mkdtemp(join(tmpdir(), "fgx-gate-"));
  const copy = join(dir, "middleware.mjs");
  await writeFile(copy, src, "utf8");
  ({ onRequest } = await import(pathToFileURL(copy).href));
});

/** Invoke the gate. `next()` stands in for "the dashboard was served". */
function call(path = "/", { method = "GET", headers = {}, body, password = PASSWORD } = {}) {
  const request = new Request(ORIGIN + path, { method, headers, body });
  return onRequest({
    request,
    env: password === undefined ? {} : { DASHBOARD_PASSWORD: password },
    next: async () => new Response(SERVED, { status: 200 }),
  });
}

function basic(pw) {
  return { Authorization: "Basic " + Buffer.from("ci:" + pw).toString("base64") };
}

const HTML = { Accept: "text/html,application/xhtml+xml" };

function setCookieValue(res) {
  const raw = res.headers.get("Set-Cookie") || "";
  return raw.split(";")[0].split("=").slice(1).join("=");
}

/** Log in through the form and return the session cookie value. */
async function login(pw = PASSWORD) {
  const res = await call("/__login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Origin: ORIGIN },
    body: new URLSearchParams({ password: pw }),
  });
  assert.equal(res.status, 303);
  return setCookieValue(res);
}

describe("fail-closed", () => {
  it("denies every request when DASHBOARD_PASSWORD is unset", async () => {
    const res = await call("/", { password: undefined });
    assert.equal(res.status, 503);
    assert.match(await res.text(), /missing DASHBOARD_PASSWORD/);
  });

  it("denies even a correct-looking Basic header when unconfigured", async () => {
    const res = await call("/", { headers: basic(PASSWORD), password: undefined });
    assert.equal(res.status, 503);
  });
});

describe("the contract smoke.py asserts", () => {
  it("unauthenticated GET / is 401 with a WWW-Authenticate challenge", async () => {
    const res = await call("/");
    assert.equal(res.status, 401);
    assert.ok(res.headers.get("WWW-Authenticate"), "challenge header must be present");
  });

  it("Basic auth with the correct password serves the dashboard", async () => {
    const res = await call("/", { headers: basic(PASSWORD) });
    assert.equal(res.status, 200);
    assert.equal(await res.text(), SERVED);
  });

  it("Basic auth with a wrong password is 401", async () => {
    const res = await call("/", { headers: basic("intentionally-wrong-password") });
    assert.equal(res.status, 401);
    assert.ok(res.headers.get("WWW-Authenticate"));
  });

  it("a malformed Basic header does not authenticate", async () => {
    for (const header of [
      { Authorization: "Basic !!!not-base64!!!" },
      { Authorization: "Basic " + Buffer.from("no-colon-here").toString("base64") },
      { Authorization: "Bearer " + PASSWORD },
      { Authorization: "Basic" },
    ]) {
      const res = await call("/", { headers: header });
      assert.equal(res.status, 401, `should reject ${JSON.stringify(header)}`);
    }
  });

  it("unauthenticated POST /api/chat is 401", async () => {
    const res = await call("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    assert.equal(res.status, 401);
  });
});

describe("browser login page", () => {
  it("serves the styled form at 401 — never 200 — and without a challenge", async () => {
    const res = await call("/", { headers: HTML });
    assert.equal(res.status, 401, "status must not change for browsers");
    assert.equal(
      res.headers.get("WWW-Authenticate"),
      null,
      "a challenge header makes the browser show its native dialog instead of the page",
    );
    const body = await res.text();
    assert.match(res.headers.get("Content-Type"), /text\/html/);
    assert.match(body, /Get inside/);
    assert.match(body, /Make your own predictions, and know what to expect from the future\./);
    assert.match(body, /name="password"/);
  });

  it("references no gated asset (it is served before auth)", async () => {
    const body = await (await call("/", { headers: HTML })).text();
    assert.doesNotMatch(body, /<link[^>]+rel=["']?stylesheet/i);
    assert.doesNotMatch(body, /<script/i);
    assert.doesNotMatch(body, /https?:\/\/(?!www\.w3\.org)/i);
  });

  it("is not cacheable by a shared cache", async () => {
    const res = await call("/", { headers: HTML });
    assert.match(res.headers.get("Cache-Control"), /no-store/);
    // The response differs per client; a cache keyed on URL alone would leak it.
    for (const key of ["Accept", "Cookie", "Authorization"]) {
      assert.match(res.headers.get("Vary"), new RegExp(key));
    }
  });

  it("GET /__login is 401, not 200", async () => {
    const res = await call("/__login", { headers: HTML });
    assert.equal(res.status, 401);
    assert.match(await res.text(), /Get inside/);
  });

  it("re-renders with an error on a wrong password, and sets no cookie", async () => {
    const res = await call("/__login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Origin: ORIGIN },
      body: new URLSearchParams({ password: "nope" }),
    });
    assert.equal(res.status, 401);
    assert.equal(res.headers.get("Set-Cookie"), null);
    assert.match(await res.text(), /Wrong password/);
  });

  it("does not leak the submitted password back into the page", async () => {
    const res = await call("/__login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Origin: ORIGIN },
      body: new URLSearchParams({ password: "<script>alert(1)</script>" }),
    });
    const body = await res.text();
    assert.doesNotMatch(body, /alert\(1\)/);
  });

  it("rejects a login POST from a foreign origin", async () => {
    const res = await call("/__login", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Origin: "https://evil.example",
      },
      body: new URLSearchParams({ password: PASSWORD }),
    });
    assert.equal(res.status, 403);
    assert.equal(res.headers.get("Set-Cookie"), null);
  });
});

describe("session cookie", () => {
  it("is issued with the hardening flags a session cookie needs", async () => {
    const res = await call("/__login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Origin: ORIGIN },
      body: new URLSearchParams({ password: PASSWORD }),
    });
    assert.equal(res.status, 303);
    assert.equal(res.headers.get("Location"), "/");
    const cookie = res.headers.get("Set-Cookie");
    for (const flag of ["HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age="]) {
      assert.ok(cookie.includes(flag), `Set-Cookie must include ${flag}: ${cookie}`);
    }
  });

  it("never contains the password", async () => {
    assert.doesNotMatch(await login(), new RegExp(PASSWORD));
  });

  it("grants access on a later request", async () => {
    const res = await call("/", { headers: { Cookie: `fgx_session=${await login()}` } });
    assert.equal(res.status, 200);
    assert.equal(await res.text(), SERVED);
  });

  it("is rejected when the signature is tampered with", async () => {
    const token = await login();
    const [exp, sig] = token.split(".");
    const flipped = sig.slice(0, -1) + (sig.endsWith("A") ? "B" : "A");
    const res = await call("/", { headers: { Cookie: `fgx_session=${exp}.${flipped}` } });
    assert.equal(res.status, 401);
  });

  it("is rejected when the expiry is extended without re-signing", async () => {
    const [, sig] = (await login()).split(".");
    const future = Math.floor(Date.now() / 1000) + 999999;
    const res = await call("/", { headers: { Cookie: `fgx_session=${future}.${sig}` } });
    assert.equal(res.status, 401);
  });

  it("is rejected once expired, even with a genuine signature", async () => {
    // A token whose exp is in the past must fail regardless of the MAC, so an
    // old cookie cannot be replayed forever.
    const res = await call("/", { headers: { Cookie: "fgx_session=1000000000.whatever" } });
    assert.equal(res.status, 401);
  });

  it("is rejected when signed with a different password", async () => {
    const token = await login();
    const res = await call("/", {
      headers: { Cookie: `fgx_session=${token}` },
      password: "rotated-password",
    });
    assert.equal(res.status, 401, "rotating DASHBOARD_PASSWORD must invalidate sessions");
  });

  it("survives other cookies sharing the header", async () => {
    const token = await login();
    const res = await call("/", {
      headers: { Cookie: `foo=bar; fgx_session=${token}; baz=qux` },
    });
    assert.equal(res.status, 200);
  });

  it("ignores junk cookie values without throwing", async () => {
    for (const value of ["", ".", "..", "abc.def", "-1.x", "x".repeat(400), "12e5.sig"]) {
      const res = await call("/", { headers: { Cookie: `fgx_session=${value}` } });
      assert.equal(res.status, 401, `should reject ${JSON.stringify(value)}`);
    }
  });

  it("logout clears the cookie", async () => {
    const res = await call("/__logout");
    assert.equal(res.status, 303);
    assert.match(res.headers.get("Set-Cookie"), /fgx_session=;/);
    assert.match(res.headers.get("Set-Cookie"), /Max-Age=0/);
  });
});

after(() => {});
