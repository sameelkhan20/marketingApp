# Meta Ads Planner

AI planner for Meta (Facebook/Instagram) sales campaigns. Give it a PKR budget,
a target country and a product; it estimates orders, writes a launch-ready plan,
learns from your edits, and (later phases) creates and monitors the campaign on Meta.

## Run

Windows, one command (creates the venv and `.env` on first run):

```powershell
.\start.ps1
```

Manual:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # add ANTHROPIC_API_KEY
python -m uvicorn backend.main:app --reload --port 8000
```

Docker (data, media and JWT secrets persist in the `planner-data` volume):

```powershell
docker compose up -d --build
```

Open http://localhost:8000. Tests (no network, temp database):

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Backup = copy the `data/` folder (SQLite DB, media, `jwt_secrets.json`).

Without `ANTHROPIC_API_KEY` the app still works but returns a template plan
(marked `[TEMPLATE]`) instead of an AI-generated one.

## Sign in

The app is protected by the auth system ported from
[core_auth_fixed](https://github.com/sameelkhan20/core_auth_fixed): JWT access +
refresh tokens signed with separate secrets, bcrypt password hashing, logout
blacklist, token rotation. Adapted to this app: users and the blacklist live in the
same SQLite database (no PostgreSQL/Redis), and endpoints sit under `/api`:

| Endpoint | Purpose |
|---|---|
| `POST /api/users/register` | owner only (owner token required); there is no public signup |
| `POST /api/auth/login` | email + password -> access (60 min) + refresh (7 days) tokens |
| `POST /api/auth/refresh` | rotate tokens |
| `POST /api/auth/logout` | blacklist the access token until it expires |
| `GET /api/users/me` | current user |

The website only has **Sign in**: no signup, even on an empty database. The owner account is
created where the app runs (this also resets a forgotten password):

```powershell
docker exec -it ads-planner-planner-1 python -m scripts.create_owner
```

Locally: `.\.venv\Scripts\python.exe -m scripts.create_owner`. JWT secrets come from
`ACCESS_TOKEN_SECRET` / `REFRESH_TOKEN_SECRET` in `.env`, or are generated once into
`data/jwt_secrets.json`. Every other `/api/*` route returns 401 without a valid token.

## Layout

```
backend/
  main.py        FastAPI routes + serves frontend
  db.py          SQLite models: plans, plan_versions, learned_rules
  benchmarks.py  country/niche CPM, CTR, CVR fallbacks
  estimator.py   budget -> order range with assumptions + warnings
  history.py     account Insights override for the estimator (after a sync)
  planner.py     Claude plan generation (structured JSON)
  learning.py    diff AI vs user versions -> rules for next plans
  meta_client.py Graph API wrapper (accounts, insights, campaign/adset/creative/ad)
  meta_sync.py   2-year insights by country -> account_stats (recent 90d weighted 2x)
  launcher.py    approved plan -> PAUSED campaign + ad sets + ads
  settings_store.py  token / account / page / pixel / store URL / image URLs
  rules.py       rules engine: plan targets vs live metrics -> findings (pure, testable)
  monitor.py     scheduled job: snapshots, alert upsert/dedupe, Claude suggestions
  notify.py      email alerts via Gmail API, Brevo fallback
  actions.py     executable suggestions (pause / budget %), guardrails, undo, auto-apply
  imagegen.py    image providers (Higgsfield default, Gemini, OpenAI)
  higgsfield.py  Higgsfield OAuth + MCP client (ported from the Social Agent)
  gmail_auth.py  in-app Gmail sign-in; secrets_store.py encrypted token storage; r2.py reference uploads
  creatives.py   prompt building from the plan's visual_prompt + product photo, file storage
  outcomes.py    predicted vs actual, per-country calibration, rules from results, action outcomes
  readiness.py   pre-launch checklist (hard blockers + warnings)
tests/           pytest suite with a fake Meta client (temp DB, no network)
data/media/      product photo and generated/uploaded ad images (served at /media)
  auth/          JWT auth ported from core_auth_fixed (security, dependencies, routes)
frontend/        vanilla HTML/CSS/JS
data/app.db      created on first run
```

## Meta connection

Open the **Meta** tab.

1. Token: easiest is a System User token from Business Settings > Users > System Users
   (assign the ad account + page, generate token with `ads_management`, `ads_read`,
   `business_management`, `pages_show_list`). Or set `META_APP_ID` / `META_APP_SECRET`
   in `.env`, whitelist `http://localhost:8000/api/meta/oauth/callback` in the app's
   Facebook Login settings, and use "Connect with Facebook".
2. Ad account id, Page id, Pixel id, store URL, and 1+ public image URLs for ads.
3. "Check connection" lists what the token can see. "Sync from Meta" pulls 2 years of
   Insights by country; those numbers then replace benchmarks in the estimator.
4. Approve a plan -> a readiness checklist appears (token valid, account currency, page,
   store URL, pixel, images, per-ad-set minimum budget). Red items block the launch,
   yellow ones only warn. "Launch on Meta (paused)" creates everything paused; nothing
   spends until you activate the campaign in Ads Manager.

Only USD and PKR ad accounts are supported for budgets right now.

## Monitoring

Every `MONITOR_INTERVAL_HOURS` (default 3) and on "Check now" in the **Monitor** tab,
the app pulls last-7-day metrics for every launched campaign (campaign, ad set, ad level)
and compares them with the plan's monitoring targets and Meta's own delivery status:

| Alert | Fires when |
|---|---|
| delivery | an ad is DISAPPROVED / WITH_ISSUES / pending review |
| no_delivery | campaign active but 0 impressions |
| under / over_pacing | 7-day spend < 60% or > 130% of plan |
| no_purchases | spend >= target CPA with 0 purchases |
| high_cpa | CPA > 1.25x target (critical at 2x) |
| low_ctr | CTR < target after 2,000 impressions |
| high_frequency | frequency > target after 1,000 impressions |
| low_roas | ROAS < target after 3 purchases |
| scale_candidate | ad set CPA <= 80% of target with 5+ purchases |
| creative_winner / creative_loser | inside an ad set, once every ad has 1,500+ impressions: best CPA (3+ purchases) or best CTR wins; ads with CPA > 1.5x the winner's (or no purchases after 2x the winner's CPA in spend), or CTR < 60% of the winner's, lose |

Alerts are deduped while open, resolved automatically when the condition clears, and
carry 2-3 suggested actions with expected effect and risk (Claude-written when
`ANTHROPIC_API_KEY` is set, canned otherwise). Paused campaigns produce no alerts.

## Email alerts

Same delivery as the Social Agent: the **Gmail API** first (Google sign-in, no password,
lands in the inbox), **Brevo** when Gmail is not set up or refuses.

1. In `.env` set `ALERT_EMAIL` (several addresses allowed, comma-separated).
2. Gmail: put the Google OAuth client in `.env` (`GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`;
   the Social Agent's Desktop-app client works), restart, then **Connect Gmail** in the Meta
   tab: sign in, copy the address the browser lands on (`http://127.0.0.1:8765/?...`, the page
   does not load) and paste it into the app. The sign-in is stored encrypted in the database.
   `scripts/set_gmail.py` still works if you prefer writing it into `.env`.

3. Optional fallback: `BREVO_API_KEY` and a Brevo-verified sender in `ALERT_FROM`.
4. Restart, then "Send test email" in the Meta tab (it says which channel was used).

After every monitor run that produces new alerts at or above `ALERT_MIN_SEVERITY`, one
email lists each alert with metrics, diagnosis, suggested actions and a dashboard link.
A **weekly digest** goes out every `DIGEST_WEEKDAY` (default Monday); "Send digest now"
sends it on demand.

## Actions and automation

Suggestions that map to a single Meta change carry an executable spec: pause / resume an
ad set or ad, or change an ad set's daily budget by a percentage. In the Monitor tab:

- **Apply** on a suggestion runs it on Meta after a confirmation. The alert is marked
  acknowledged.
- **Action log** lists every change (manual, auto, undo) with the state before it;
  **Undo** reverts it.
- **Automation** (off by default): per alert type, let the monitor run the first
  executable option itself when a new alert of that type appears.

Creative testing adds two more executable actions on ads: **clone winner** (create the
winning ad, same creative, in every other ad set, paused) and **refresh creative**
(generate a fresh AI image with the same copy, upload it, add it as a paused ad in the
same ad set). New ads are always created paused; refresh is never automatic because it
spends image credits. Undo deletes the ads that were created.

Guardrails apply to manual and automatic actions alike: only ad sets and ads are ever
changed (never the campaign); budget changes are clamped to the "max budget change per
action" setting (default 20%), never below ~2x Meta's minimum daily budget and never
above the plan's total daily budget; resume is never automatic. Claude-written options
also carry specs only when they describe exactly a pause or a budget change on the
alert's own object.

## AI creatives

Each plan creative now has a `visual_prompt` written by Claude (scene, model, lighting,
composition; never text, logos or celebrity faces). On the plan page, "Ad images":

1. Upload your real **product photo** once (reference) and optionally a brand style line.
2. Per creative: **Generate with AI** (uses the product photo as reference so the jacket
   stays accurate; aspect auto from the format or 1:1 / 4:5 / 9:16) or **Upload image**.
3. Selected images are uploaded to Meta's image library on launch and used as the ads'
   `image_hash`; the Meta-tab image URLs remain a fallback.

Provider, same as the Social Agent: **Higgsfield** by default (`IMAGE_PROVIDER=higgsfield`),
using the team's Higgsfield plan credits through Higgsfield's MCP server. Connect it once
with **Connect Higgsfield** in the Meta tab (OAuth; paste back the `http://localhost:8765/callback?...`
address). The product photo is put on the R2 bucket (`R2_*` in `.env`, the Social Agent's
public bucket works) for the length of one job and removed afterwards. Billing safety from
the Social Agent: a picture request is resent only on 503, and once a job has started,
waiting/downloading errors are retried for up to 6 minutes instead of discarding a paid
picture. Model settings: `HIGGSFIELD_MODEL`, `_VARIANT`, `_QUALITY`, `_RESOLUTION`.
Alternatives: `IMAGE_PROVIDER=gemini` or `openai` with `IMAGE_API_KEY`.
Video is not generated; use Reels made from the images or your own footage.

## Learning from outcomes

Every monitor run also records the launch's lifetime spend and purchases and compares
them with the estimate, prorated by days active (ahead / on track / behind). When a
launch completes (duration reached, or paused after 7+ days, or "Close & learn"):

- the ratio actual / predicted updates a **per-country calibration factor** (clamped
  0.5-2.0) that multiplies future estimates for that country; after 3 launches a
  benchmark-only estimate is upgraded from low to medium confidence;
- the best / weakest ad sets and ads become **learned rules** tagged
  `[result <country> <niche>]` (Claude-written when available), injected into the next plan;
- the weights of existing rules move with the verdict (ahead +0.2, on track +0.1, behind -0.2).

Each Apply / auto action is evaluated 3 days later (object CPA before vs after, campaign
CPA for pauses) and shown as improved / neutral / worse in the action log; the aggregate
per action type is shown on the Learned rules page to inform what to automate.

## Roadmap

1. Planner + estimate (done)
2. Approve / edit / versioning + learning loop (done)
3. Meta connect, 2-year history sync, paused launch (done, needs real credentials to test)
4. Monitoring job + dashboard alerts (done)
5. Email alerts with suggested actions (done)
6. Apply / undo / opt-in automation with guardrails (done)
7. AI creative images with product-photo reference (done)
8. Learning from outcomes: calibration, result rules, action outcomes (done)
9. Creative testing loop: winner/loser detection, clone winner, refresh creative (done)
10. Production readiness: pytest suite, launch checklist, weekly digest, Docker/start script (done)
11. Ideas: AI video creatives, multi-account, Meta Experiments API for formal lift tests
