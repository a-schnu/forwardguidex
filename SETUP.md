# ForwardGuidex — cloud setup guide

One-time owner setup to take the repo from "code complete" to "live at
`app.forwardguidex.com`". Work top to bottom. Each step says what to **run**,
what to **capture** (values that become GitHub secrets/vars), and how to
**verify**. Collect all captured values in step 7.

Prerequisites:
- A GCP account with billing enabled, and the domain **forwardguidex.com** on Cloudflare.
- CLIs: `gcloud`, `firebase` (`npm i -g firebase-tools`), `npx wrangler`, `gh` (GitHub CLI). `git bash` to run these.
- You are `a-schnu` and the repo is `a-schnu/forwardguidex`.

Set these shell variables first (adjust `PROJECT_ID` — it must be globally unique):

```bash
export PROJECT_ID="forwardguidex-prod"          # your GCP project id (globally unique)
export GH_REPO="a-schnu/forwardguidex"
export FIRESTORE_LOCATION="eur3"                # multi-region: eur3 (Europe) or nam5 (US)
```

---

## Step 1 — GCP project + Firestore (Native)

```bash
gcloud auth login
gcloud projects create "$PROJECT_ID" --name="ForwardGuidex"
gcloud config set project "$PROJECT_ID"
# Link billing (list accounts, then link one):
gcloud billing accounts list
gcloud billing projects link "$PROJECT_ID" --billing-account=XXXXXX-XXXXXX-XXXXXX

# Enable APIs:
gcloud services enable firestore.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com iam.googleapis.com

# Create Firestore in Native mode:
gcloud firestore databases create --location="$FIRESTORE_LOCATION"

# Capture the project NUMBER (needed for WIF):
export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
echo "PROJECT_NUMBER=$PROJECT_NUMBER"
```

**Capture:** `PROJECT_ID` (→ GitHub **variable** `GOOGLE_CLOUD_PROJECT`), `PROJECT_NUMBER`.
**Verify:** `gcloud firestore databases list` shows a `(default)` database.

---

## Step 2 — Workload Identity Federation + service accounts

```bash
# 2a. Pool + GitHub OIDC provider, restricted to your repo:
gcloud iam workload-identity-pools create github-pool \
  --location=global --display-name="GitHub Actions Pool"

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global --workload-identity-pool=github-pool \
  --display-name="GitHub provider" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='${GH_REPO}' && assertion.ref=='refs/heads/main'"

# 2b. Two service accounts (least privilege):
gcloud iam service-accounts create fgx-writer --display-name="ForwardGuidex Firestore writer"
gcloud iam service-accounts create fgx-reader --display-name="ForwardGuidex Firestore reader"

export WRITER_SA="fgx-writer@${PROJECT_ID}.iam.gserviceaccount.com"
export READER_SA="fgx-reader@${PROJECT_ID}.iam.gserviceaccount.com"

# 2c. Roles: writer can create docs, reader is read-only:
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${WRITER_SA}" --role="roles/datastore.user"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${READER_SA}" --role="roles/datastore.viewer"

# 2d. Let the GitHub repo impersonate each SA via WIF:
export PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/${GH_REPO}"
gcloud iam service-accounts add-iam-policy-binding "$WRITER_SA" \
  --role="roles/iam.workloadIdentityUser" --member="$PRINCIPAL"
gcloud iam service-accounts add-iam-policy-binding "$READER_SA" \
  --role="roles/iam.workloadIdentityUser" --member="$PRINCIPAL"

# 2e. The provider resource name (this is the WIF_PROVIDER secret):
echo "WIF_PROVIDER=projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/providers/github-provider"
echo "WIF_WRITER_SA=${WRITER_SA}"
echo "WIF_READER_SA=${READER_SA}"
```

**Capture:** `WIF_PROVIDER`, `WIF_WRITER_SA`, `WIF_READER_SA`.

---

## Step 3 — Deploy Firestore rules + indexes

```bash
firebase login
firebase use "$PROJECT_ID"     # or: firebase deploy --project "$PROJECT_ID" ...
firebase deploy --only firestore:rules,firestore:indexes --project "$PROJECT_ID"
```

**Verify:** GCP console → Firestore → Rules shows `allow read, write: if false;`.
The composite index (`snapshots_history`) may take a minute to build.

---

## Step 4 — Cloudflare Pages + DNS

Two Pages projects. Do these in the Cloudflare dashboard (Workers & Pages):

1. **`fgx-landing`** — *Connect to Git* → your `forwardguidex` repo. Build settings:
   framework preset **None**, build command **(empty)**, build output directory **`site`**.
   Add custom domain **`www.forwardguidex.com`**.
2. **`fgx-dashboard`** — *Direct Upload* (create empty; CI uploads `app/` on each deploy).
   Add custom domain **`app.forwardguidex.com`**.

**API token** (My Profile → API Tokens → Create Token → *Edit Cloudflare Pages* template,
scoped to your account):

**Capture:** `CLOUDFLARE_API_TOKEN`, and `CLOUDFLARE_ACCOUNT_ID` (right sidebar of any
domain overview / Workers & Pages page).

DNS records are created automatically when you add the custom domains above (CNAMEs to
`*.pages.dev`, proxied).

---

## Step 5 — Cloudflare Access (Zero Trust) + service token

Zero Trust → Access → Applications → **Add a self-hosted application**:
- Application domain: **`app.forwardguidex.com`**
- Policy: **Allow**, include your email (One-time PIN), everyone else denied.

Zero Trust → Access → **Service Auth** → Create Service Token → name `fgx-ci-smoke`.
Then add an Access policy on the app that **includes** this service token (so CI can pass).

**Capture:** `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` (shown once at creation).

---

## Step 6 — NY Fed disclaimer

Replace the placeholder in `legal/nyfed-reference-rates.txt` with the **verbatim**
reference-rate disclaimer from the NY Fed Terms of Use
(https://www.newyorkfed.org/markets/reference-rates/terms-of-use), remove the
`[[REPLACE-WITH-OFFICIAL-NYFED-DISCLAIMER]]` sentinel line, then bump
`disclaimer_version` in `config/sources.yaml`. Commit + push.

---

## Step 7 — GitHub secrets & variables

Using the values captured above (`gh` CLI shown; or use repo Settings → Secrets and variables → Actions):

```bash
gh variable set GOOGLE_CLOUD_PROJECT --repo "$GH_REPO" --body "$PROJECT_ID"

gh secret set WIF_PROVIDER   --repo "$GH_REPO" --body "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/providers/github-provider"
gh secret set WIF_WRITER_SA  --repo "$GH_REPO" --body "$WRITER_SA"
gh secret set WIF_READER_SA  --repo "$GH_REPO" --body "$READER_SA"

gh secret set CLOUDFLARE_API_TOKEN   --repo "$GH_REPO"      # paste when prompted
gh secret set CLOUDFLARE_ACCOUNT_ID  --repo "$GH_REPO"
gh secret set CF_ACCESS_CLIENT_ID    --repo "$GH_REPO"
gh secret set CF_ACCESS_CLIENT_SECRET --repo "$GH_REPO"

gh secret set OPENROUTER_API_KEY  --repo "$GH_REPO"
gh secret set OPENROUTER_MODEL    --repo "$GH_REPO" --body "anthropic/claude-3.5-sonnet"
gh secret set TELEGRAM_BOT_TOKEN  --repo "$GH_REPO"
gh secret set TELEGRAM_CHAT_ID    --repo "$GH_REPO"
```

Also create the protected environment: repo Settings → Environments → **New environment
`production`** (optionally add yourself as a required reviewer).

---

## Step 8 — Seed the first live snapshot

Actions → **forwardguidex-daily** → **Run workflow** (branch `main`). It runs
ingest → export → validate → deploy → smoke → archive. When green:
- `www.forwardguidex.com` = public landing.
- `app.forwardguidex.com` = dashboard (prompts for Access email OTP).

Then test a rollback once (induce a smoke failure) to confirm the safety net, and point
the landing "Apri la dashboard" button (already `https://app.forwardguidex.com`).

---

### Checklist of GitHub secrets/vars

| Secrets | Variable |
|---|---|
| `WIF_PROVIDER`, `WIF_WRITER_SA`, `WIF_READER_SA`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `GOOGLE_CLOUD_PROJECT` |
