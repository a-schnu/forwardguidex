/* ForwardGuidex — snapshot loader (security-critical).
 *
 * Flow:
 *   1. Resolve manifest: data/latest.json, falling back to data/latest.demo.json.
 *   2. Fetch the named snapshot as raw bytes (ArrayBuffer).
 *   3. Compute SHA-256 of those exact bytes and compare to manifest.artifact_sha256.
 *      On mismatch: REFUSE to render (return an integrity-error state) — never parse.
 *   4. Only then decode (TextDecoder) + JSON.parse.
 *   5. Enforce meta.schema_version === 1.
 *   6. Freshness is DOWNGRADE-ONLY (FRESH -> STALE by wall-clock age; never STALE -> FRESH).
 *
 * No external network, no Firebase, no inline code. connect-src is 'self' only.
 */

export const SCHEMA_VERSION = 1;

// EOD cadence: tolerate weekends/holidays before declaring the data stale.
const STALE_AFTER_MS = 96 * 60 * 60 * 1000; // 96h

function bufferToHex(buffer) {
  const bytes = new Uint8Array(buffer);
  const out = new Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) {
    out[i] = bytes[i].toString(16).padStart(2, '0');
  }
  return out.join('');
}

async function fetchJson(url) {
  const res = await fetch(url, { cache: 'no-store', credentials: 'same-origin' });
  if (!res.ok) {
    throw new Error('HTTP ' + res.status + ' for ' + url);
  }
  return res.json();
}

async function resolveManifest() {
  // Production manifest first; fall back to the explicit demo manifest for local dev.
  try {
    const manifest = await fetchJson('data/latest.json');
    return { manifest, source: 'prod' };
  } catch (_e) {
    const manifest = await fetchJson('data/latest.demo.json');
    return { manifest, source: 'demo' };
  }
}

// Defence-in-depth: the snapshot filename must be a plain data-dir file, no traversal.
function isSafeSnapshotName(name) {
  return typeof name === 'string' && /^snapshot\.[A-Za-z0-9._-]+\.json$/.test(name)
    && name.indexOf('/') === -1 && name.indexOf('\\') === -1;
}

/**
 * Downgrade-only freshness. Mutates meta.freshness FRESH -> STALE when the effective
 * data timestamp is older than the tolerance. Never upgrades STALE -> FRESH.
 * Returns whether a downgrade was applied (for cosmetic display only).
 */
function applyFreshnessDowngrade(meta) {
  if (meta.freshness !== 'FRESH') return false;
  const anchor = Date.parse(meta.data_as_of);
  if (!Number.isFinite(anchor)) return false;
  if (Date.now() - anchor > STALE_AFTER_MS) {
    meta.freshness = 'STALE';
    return true;
  }
  return false;
}

/**
 * Cosmetic-only US market-state hint. NEVER gates loading or rendering.
 * Returns { open, label } derived from wall-clock time in America/New_York.
 * Any failure yields a neutral result.
 */
export function marketStateCosmetic(now) {
  try {
    const d = now || new Date();
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false
    }).formatToParts(d);
    const get = (t) => (parts.find((p) => p.type === t) || {}).value;
    const wd = get('weekday');
    let hh = parseInt(get('hour'), 10);
    if (hh === 24) hh = 0;
    const mm = parseInt(get('minute'), 10);
    const isWeekday = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].indexOf(wd) !== -1;
    const minutes = hh * 60 + mm;
    const open = isWeekday && minutes >= 9 * 60 + 30 && minutes < 16 * 60; // 09:30–16:00 ET
    return { open: open, label: open ? 'Mercato USA aperto' : 'Mercato USA chiuso' };
  } catch (_e) {
    return { open: false, label: '' };
  }
}

/**
 * Load, verify and parse the snapshot.
 * Resolves to one of:
 *   { status: 'ok', snapshot, manifestSource, freshnessDowngraded }
 *   { status: 'integrity-error', expected, actual }
 *   { status: 'schema-error', schemaVersion }
 *   { status: 'load-error', message }
 */
export async function loadSnapshot() {
  let manifest, manifestSource;
  try {
    const resolved = await resolveManifest();
    manifest = resolved.manifest;
    manifestSource = resolved.source;
  } catch (e) {
    return { status: 'load-error', message: 'Manifest non raggiungibile: ' + e.message };
  }

  if (!manifest || typeof manifest.artifact_sha256 !== 'string' || !isSafeSnapshotName(manifest.snapshot)) {
    return { status: 'load-error', message: 'Manifest non valido.' };
  }

  let buffer;
  try {
    // Snapshot files are content-addressed & immutable — the browser may cache them.
    const res = await fetch('data/' + manifest.snapshot, { cache: 'force-cache', credentials: 'same-origin' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    buffer = await res.arrayBuffer();
  } catch (e) {
    return { status: 'load-error', message: 'Snapshot non raggiungibile: ' + e.message };
  }

  let actualHex;
  try {
    const digest = await crypto.subtle.digest('SHA-256', buffer);
    actualHex = bufferToHex(digest);
  } catch (e) {
    return { status: 'load-error', message: 'Verifica hash non riuscita: ' + e.message };
  }

  const expectedHex = String(manifest.artifact_sha256).toLowerCase();
  if (actualHex !== expectedHex) {
    // Refuse to render stale/corrupt bytes.
    return { status: 'integrity-error', expected: expectedHex, actual: actualHex };
  }

  let snapshot;
  try {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(buffer);
    snapshot = JSON.parse(text);
  } catch (e) {
    return { status: 'load-error', message: 'Snapshot non decodificabile: ' + e.message };
  }

  if (!snapshot || !snapshot.meta || snapshot.meta.schema_version !== SCHEMA_VERSION) {
    return {
      status: 'schema-error',
      schemaVersion: snapshot && snapshot.meta ? snapshot.meta.schema_version : undefined
    };
  }

  const freshnessDowngraded = applyFreshnessDowngrade(snapshot.meta);

  return {
    status: 'ok',
    snapshot: snapshot,
    manifestSource: manifestSource,
    freshnessDowngraded: freshnessDowngraded
  };
}
