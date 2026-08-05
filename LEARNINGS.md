# PropertyScout — Operational learnings & gotchas

*Hard-won notes from the Aug 2026 audit + build sessions. Read this before touching
the data pipelines, the GitHub Actions workflows, the git/PR flow, or anything that
fetches from an external service. Every item here cost real debugging time.*

---

## GitHub Actions & workflows

- **An unset secret arrives as an empty string, not "absent".** In a workflow,
  `env: FOO: ${{ secrets.FOO }}` sets `FOO=""` when the secret doesn't exist. So in
  Python, `os.environ.get("FOO", default)` returns `""`, **not** the default. Use
  `os.environ.get("FOO") or default`. *(This silently made the EPC build query zero
  councils and return 0 rows — the single most confusing bug of the session.)*
- **`continue-on-error: true` shows a green tick even when the step failed.** A broken
  sub-step can look successful. If a step can "succeed with nothing", make it print
  loud diagnostics and `exit 1` on empty output, so failures are visible.
- **"Works in my browser / robots.txt allows it" ≠ "works from GitHub's servers".**
  Government and anti-bot sites block or redirect **data-centre IPs**. Symptoms seen:
  HM Land Registry live API returns 403; the EPC bulk API returned an HTTP 200 GOV.UK
  **HTML sign-in page** instead of JSON. Always verify a source is reachable *from
  Actions*, not just from a phone, before building on it. **Prefer download-once,
  query-a-local-file** (INSPIRE, OS, Price Paid, UPRN, EPC bulk — none of these ever
  failed).
- **Diagnostics-first when a call "returns nothing".** Print HTTP status, Content-Type,
  and the first ~200 chars of the body. That one line ("body starts: `<!DOCTYPE html>…
  govuk-template`") is what finally revealed the EPC service had moved.

## External data sources (current state Aug 2026)

- **The EPC bulk service moved.** `epc.opendatacommunities.org` (Basic-auth email+key,
  bulk CSV/JSON) appears retired/redirected. The current service is
  **`get-energy-performance-data.communities.gov.uk`**:
  - *Per-property API* (Bearer token = `EPC_API_KEY`) — what `run.py`'s live lookups use.
    Its `/api/domestic/search` does **not** return floor area (you must fetch each
    certificate), so it **cannot** do bulk efficiently.
  - *Bulk download* — sits behind **GOV.UK One Login** (interactive), so it can't be
    fetched automatically. **`epc_region.json.gz` must be built by a manual download +
    upload** (see `epc_upload/README.md` + the "Build house-size file from upload"
    workflow).
- **The two EPC keys are different services.** `EPC_API_KEY` (Bearer, per-property,
  proven nightly) is **not** interchangeable with an opendatacommunities bulk key.
  Don't assume one works for the other.
- **HM Land Registry Price Paid live API 403s cloud IPs** — never call it from Actions;
  use the local `price_paid_region.json.gz`. (The old live fallbacks were removed.)
- **Rate-limit discipline is load-bearing.** Circuit-breaker (`_DEAD`/`_kill`) parks a
  source on its first 429/403; Gazette obeys a 10s crawl delay + `Accept: text/html` on
  notice pages; constraints/planning results are cached per point. Extend the breaker to
  any new source. A throttle heals; a ban is fatal.

## Git & pull-request flow

- **The nightly bot commits binary `data/scout.db` (+ `docs/properties.json`) to `main`
  every run.** A binary DB can't auto-merge, so any long-lived branch eventually hits a
  **merge conflict on `data/scout.db`**. Pre-empt: keep branches short-lived and merge
  `main` in (or rebase) before opening a PR. To resolve: take `main`'s newest `scout.db`
  (`git checkout --theirs`), then **re-run the PII scrub + `VACUUM`**, and scrub
  `properties.json` too, before committing.
- **Commits pushed to a branch *after* its PR is merged are NOT in `main`** — they need a
  **new** PR. (This caused a "PR #5 has no merge button" confusion: #5 was already
  merged; the later commit needed #6.) Don't push follow-ups onto a just-merged branch;
  open a fresh PR.
- **After a PR merges, restart the branch from latest `main`** for follow-up work
  (`git fetch origin main && git checkout -B <branch> origin/main`). `--force-with-lease`
  is fine when the branch only carried already-merged history.

## Data files & the engine

- **Optional local bulk files must degrade gracefully.** `uprn_coords.json.gz` and
  `epc_region.json.gz` are both no-ops when absent (centroid geocoding / type-matched
  comps). This let features ship dormant and switch on by upload. Keep this pattern.
- **A bulk file should carry the UPRN.** A UPRN-less EPC file will *silently disable
  precise geocoding* for any subject it resolves, unless guarded. `subject_epc` now only
  short-circuits the live lookup when the bulk entry **also** has a UPRN — so floor-area
  and precise-location come together, never one at the cost of the other.
- **PAON matching is exact.** Comps/subjects are keyed `"<canonical-postcode>|<PAON>"`
  upper-cased, where PAON is the Price-Paid house number or name. Truncated names in a
  bulk file (e.g. a 14-char cutoff) mean *named* houses won't match; *numbered* houses —
  the majority — match cleanly. When building an EPC file, keep the **full** PAON.
- **No data beats bad data.** Drop/flag rather than display wrong numbers: implausible
  plots rejected (`MAX_PLAUSIBLE_*`), EPC never matched to a neighbour, unverified plots
  flagged `approx-location` (suppressed once a UPRN-precise coordinate is used).

## Privacy / GDPR

- **The repo is public (free GitHub Pages), so EVERYTHING committed is public** — both
  `docs/properties.json` *and* `data/scout.db` and the root data files. The passcode gate
  is **cosmetic**. Person-level data (deceased names, executor/solicitor contacts, notice
  text, precise identifiers) must be stripped from anything committed — that's what
  `_public_safe()` does, applied at both the JSON publish and the DB write. Open
  government data (EPC floor areas, Price Paid) is acceptable to commit (already public).
  The real fix for genuine privacy is **private hosting**.

## Deploy reality (phone-only, non-coder)

- The user deploys via the **GitHub web UI on a phone** and can't run scripts locally.
  So converters need a workflow **button** (manual `workflow_dispatch`) or a manual
  download→upload flow — not "run this locally".
- **GitHub web upload caps at ~25 MB per file.** Matters for manual data uploads (split
  if larger).
- Give steps as literal taps; expect the government/site UI to differ from any written
  steps, and ask for a screenshot rather than guessing.

---

*Add to this file whenever something bites. It's cheaper to read than to rediscover.*
