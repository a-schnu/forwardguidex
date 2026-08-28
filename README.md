# ForwardGuidex — EOD Market Dashboard

**Forward guidance per tutto il tuo universo d'investimento.**

A personal **market-intelligence warehouse** and **end-of-day (EOD) dashboard**. It ingests your
investment universe — indexes, futures, sector stocks/ETFs, US Treasury yields, NY Fed reference
rates and news/geopolitics — lands it in a DuckDB warehouse, and turns it into a **statically
published**, hash-verified snapshot plus a data-driven, LLM-written **Morning Brief** on Telegram.

Data is **end-of-day and static** — "Live"/real-time is reserved for a future licensed-streaming
architecture, so nothing here implies intraday freshness.

Built for a **dual horizon**: active leveraged long/short trading *and* long-term buy-and-hold
investing, in stocks and ETFs.

> Decision-support / research tool. It surfaces facts, moves and context — it does **not** place
> trades or give personalized investment advice. Leverage cuts both ways. **Non è consulenza.**

## Architecture

```
SOURCES                 INGEST            WAREHOUSE          INTELLIGENCE       PUBLISH / SERVE
yfinance        ─┐                     ┌ raw_prices ┐                        ┌ export → snapshot.<hash>.json
US Treasury      ┼─ Python connectors ─┤ raw_macro  ├─ DuckDB marts ─ LLM brief ┤ validate (fail-closed)
NY Fed Markets   ┤   (fwdx CLI)        └ raw_news   ┘  (gold_*)  (OpenRouter) ┤ Cloudflare Pages (Direct Upload)
GDELT           ─┘                                                            ┤ smoke test (Access token)
                                                                             └ Firestore archive (create-only, WIF)
```

- **Landing** → `www.forwardguidex.com` (Cloudflare Pages, Git integration, `site/`).
- **Dashboard** → `app.forwardguidex.com` (Cloudflare Pages, Direct Upload, `app/`), gated by
  **Cloudflare Access** (owner email) for the private Phase 0A launch.
- **Delivery is static-first**: CI builds → validates → uploads → same-origin `fetch`. No Firebase
  SDK or public DB rules ever run in the browser.

Release states: `BUILT → VALIDATED → DEPLOYED → SMOKE_TESTED → ARCHIVED` — or, when the delivery
rather than the data breaks, `BUILT → VALIDATED → VALIDATED_NOT_DEPLOYED → ARCHIVED`
(deploy ≠ archive; a smoke-test failure rolls back code + data together; a shared concurrency
lock spans select → validate → deploy → smoke → rollback/success).

Two — and only two — statuses ever reach the archive, and they are not interchangeable:

- **`SMOKE_TESTED`** — deployed to Cloudflare and verified live by the authenticated smoke test.
  The **only** status eligible for rollback selection.
- **`VALIDATED_NOT_DEPLOYED`** — passed `fwdx validate`, but never made it live: the Cloudflare
  deploy failed, or smoke failed and the deployment was rolled back. Archiving it closes the hole
  in the history on days when the delivery broke, and it can **never become a rollback target** —
  the record contract accepts only `SMOKE_TESTED`.

Nothing that failed *validation* is ever archived. Full contract — including why a byte-identical
same-day re-run can leave `release_status` understated (never overstated) — in the
[`serve/publish.py`](src/forwardguidex/serve/publish.py) module docstring.

## Setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"     # add ".[publish]" for the Firestore archive step (firebase-admin)
cp .env.example .env         # then fill in keys
```

### Dependency locks

CI never resolves dependency ranges: it installs from hash-pinned lockfiles with
`pip install --require-hashes --only-binary=:all:`, so an upstream release cannot reach production
without an explicit, reviewed lock change.

```bash
pip install uv
./scripts/lock-deps.sh      # regenerates requirements/{core,publish,dev}.lock for cp311/manylinux
```

Run it after **any** edit to `[project.dependencies]` or an optional-dependency group, and commit
`requirements/*.lock` in the same commit as `pyproject.toml` — the `locks` job in
[`ci.yml`](.github/workflows/ci.yml) regenerates them and fails on drift.

Keys: [OpenRouter](https://openrouter.ai/keys) and a Telegram bot via @BotFather.
**US Treasury and NY Fed need no key. GDELT needs no key. FRED is no longer used** (removed for
compliance — its terms conflict with persistence + LLM use).

## Usage

```bash
fwdx init                       # create DB + ticker dimension
fwdx ingest all                 # markets (yfinance) + rates (UST + NY Fed) + news (GDELT)
fwdx marts                      # build gold marts (returns, sector rollups, latest rates)
fwdx brief                      # build + print the Morning Brief (needs OpenRouter key)
fwdx send-brief                 # build + send it to Telegram (with Treasury + NY Fed notices)
fwdx run-daily                  # ingest -> marts -> brief -> telegram (one shot)

fwdx export --out-dir out       # build snapshot.<hash>.json + latest.json from the warehouse
fwdx export --demo --out-dir app/data   # (re)generate the local demo bundle
fwdx validate out/snapshot.*.json       # fail-closed validation (non-zero exit on any error)
fwdx publish out/snapshot.*.json        # archive to Firestore (create-only, via WIF; needs [publish])
fwdx decommission-fred          # one-time FRED cleanup (idempotent)
```

Open `app/index.html` (via a local static server) to preview the dashboard against the committed
demo bundle in `app/data/`.

## Configure your universe

Everything (tickers, sectors, Treasury maturities, NY Fed rates, GDELT queries) lives in
[`config/universe.yaml`](config/universe.yaml). Source-rights policy lives in
[`config/sources.yaml`](config/sources.yaml). Focus sectors: Oil & Gas, Defense & Aerospace,
Consumer Staples, Tech Software, Tech Hardware/Semis, Infrastructure & Industrials.

## Design decisions (ADRs)

**Two hashes.** `content_hash` = SHA-256 of the **canonical payload** (`sort_keys`, compact, UTF-8,
`allow_nan=False`) with `meta.content_hash` removed then re-inserted — the semantic identity.
`artifact_sha256` = SHA-256 of the **exact serialized final file bytes** — used in the filename and
verified byte-for-byte by the browser (`crypto.subtle.digest`). The browser refuses to render on any
mismatch, so a flipped byte can never be shown as real data. This avoids cross-language
canonicalization as the trust root.

**Fail-closed validation.** `fwdx validate` rejects on: JSON-schema violation (Draft 2020-12,
`additionalProperties:false`, bounds, `FormatChecker`), NaN/Inf, out-of-range / anomalous values,
non-https URLs, missing coverage, **per-asset-class staleness**, **source-rights** violation, demo
payload in a non-demo mode, >750 KiB, or a manifest/hash mismatch. Any failure → non-zero exit → the
deploy is blocked.

**Per-asset-class freshness.** Each class has its own exchange calendar and tolerated session lag
(equities → NYSE, futures → CME, UST → SIFMA, EFFR/SOFR → SIFMA +1 session, news → wall-clock).
Weekends, holidays, early closes and DST are handled by comparing against real `market_close` times
in UTC. See [`serve/calendar.py`](src/forwardguidex/serve/calendar.py). Server sets freshness; the
browser may only **downgrade** it, never upgrade.

**Machine-enforced source-rights.** [`config/sources.yaml`](config/sources.yaml) records, per source,
`approval_status` / `allowed_modes` / `allowed_uses` / `evidence_reference` / `review_expires_at`.
The validator computes the `(source, use)` pairs a snapshot actually needs and rejects anything not
explicitly approved for the current `deployment_mode`. Uses are distinct — a dashboard approval does
not imply telegram/ai_input/persistence. Going public requires an explicit approved review for a
`PUBLIC_*` mode; it cannot happen by DNS/config alone.

**NY Fed / Treasury notices.** Attribution + the NY Fed disclaimer are **version-controlled** in
[`legal/`](legal/) and injected wherever EFFR/SOFR appear (snapshot `meta.attribution`, dashboard
footer, Telegram brief). They are never LLM-generated or paraphrased.
> ⚠️ Before any public surface, replace `legal/nyfed-reference-rates.txt` with the **verbatim**
> disclaimer from the NY Fed Terms of Use and bump `disclaimer_version` in `config/sources.yaml`.

## Deploy runbook (owner performs cloud setup)

1. **GCP/Firebase** — project → Firestore (Native). No Firebase Hosting.
2. **WIF** — pool + GitHub OIDC provider restricted to the owner + exact repo + `refs/heads/main` +
   `production` env; a **writer** SA (Firestore create) and a **reader** SA (read-only); bind repo → SA;
   store the provider + SA emails as vars/secrets (no key file).
3. **Rules** — deploy the deny-all [`firestore.rules`](firestore.rules).
4. **Cloudflare** — Pages:Edit token → `CLOUDFLARE_API_TOKEN` (+ account id); create `fgx-landing`
   (Git integration, `site/`, `www`) and `fgx-dashboard` (Direct Upload, `app`); DNS.
5. **Access** — self-hosted Access app on `app.` (owner email OTP) **[mandatory]**; an Access
   **service token** → `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` in the `production` env for CI
   smoke tests.
6. **GitHub** — push; add secrets (`OPENROUTER_API_KEY`, Telegram, Cloudflare, WIF, Access token);
   protected `production` environment. No FRED key needed.
7. **Seed** — run `daily` (`workflow_dispatch`) → validated snapshot deployed + smoke-tested + archived.
8. Point the landing "Apri la dashboard" button → `https://app.forwardguidex.com`; test a rollback once.

## Automation

- [`.github/workflows/daily.yml`](.github/workflows/daily.yml) — weekday pre-open cron:
  `run-daily → export → validate → Direct Upload → smoke → publish (WIF)`.
- [`.github/workflows/deploy-app.yml`](.github/workflows/deploy-app.yml) — on `app/**` push /
  manual: revalidate last-known-good → Direct Upload → smoke → rollback on failure.
- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — every push / PR: `ruff` + the full test
  suite, plus a lockfile-drift check. Secretless and side-effect free.

Both share the `forwardguidex-production-deployment` concurrency group (`cancel-in-progress:false`),
run in the protected `production` environment, use SHA-pinned actions, and grant `id-token:write`
only on the WIF job.

## Roadmap / deferred

- **Deferred:** international sovereign yields (primary sources), public redistribution
  (source-rights → public mode), browser-live/streaming (licensed feed + narrowed rules + App Check),
  private portfolio / personalized recommendations / automated execution.
- The orthogonal status envelope + manifest layout make the live path a drop-in, not a rewrite.

## License

MIT
