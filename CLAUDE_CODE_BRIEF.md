# PropertyScout — Briefing for Repo Audit

*Read this first. It explains what this tool is, why it's built the way it is, the constraints that shaped it, and where it's going — so your analysis is objective and aligned with intent, not generic.*

---

## Your task

Audit this repository for **bugs, fragilities, accuracy problems, and improvement opportunities**. Read the whole codebase, understand the intent below, then produce a prioritised assessment. Be critical and specific. I want you to challenge decisions where the code doesn't serve the stated purpose — but understand the constraints (below) before recommending changes, because several "obvious" fixes have already been tried and rejected for real reasons.

Please deliver, in order:
1. **Correctness bugs** — things that are wrong or will break, most severe first.
2. **Accuracy weaknesses** — where the data or scoring misleads the user, with the fix.
3. **Fragility / architecture risks** — what will break next, and why.
4. **Improvement opportunities** — ranked by value-to-effort, aligned to the vision below.
5. **Anything I've missed** — blind spots in the whole approach.

For each item: what it is, where (file/function), why it matters, and a concrete recommendation. Flag anything that would put my API access at risk (see constraints) as high priority.

---

## What this tool is

**PropertyScout is a personal property-hunting radar for one user (me).** It is NOT a product, not a SaaS, not for anyone else. It runs itself for free on GitHub Actions (nightly cron) and publishes a private, passcode-gated web page I open on my phone.

**The core thesis — one scoring lens over BOTH the public market AND an off-market edge.**
The tool should ingest the *entire* publicly-available for-sale market (Rightmove, Zoopla, OnTheMarket, etc.) AND supplement it with a scraped/derived layer of **pre-market and unseen** opportunities. Everything — public listing and off-market lead alike — runs through the SAME scoring so they compete in one ranked list.
- **Public for-sale listings** — the baseline everyone can see, but re-scored through my lens (development potential, plot, value-vs-comparable, edge). This is the bulk of the market and must be covered.
- **Off-market edge (the moat)** — probate/deceased-estate notices (The Gazette), auction lots, and plot/price anomalies. Properties that will hit the market soon, empty houses with motivated executors, or things the masses haven't seen. This is the differentiator on top of the public stock.
The point is NOT to ignore the portals — it's to see *everything*, scored consistently, with an off-market advantage layered on.

**IMPORTANT — current gap vs this intent:** live public-listing coverage currently comes only via Homedata (quota-limited, often empty). Comprehensive, reliable coverage of the public for-sale market is a KEY requirement that is under-served today. Assess how to achieve broad public-listing ingestion within the constraints (see the hard lessons — scraping portals directly has ToS/anti-bot/licensing risks that must be weighed).

**What I'm actually looking for:** a forever home with development potential. Detached, large plot, in a nice area near Farnham/Wrecclesham, Surrey, roughly £300k–£900k, ideally undervalued or with an angle (extend, convert, subdivide, barn/outbuilding conversion). Low competition matters more than move-in-ready.

**Direct comparison to MY current home is a first-class feature.** Every property — public listing or off-market lead — should be measured directly against my own home's floor area and plot size, and that comparison shown prominently (e.g. "+40% floor area, +210% plot vs yours"). This is the buyer's-eye judgement I actually make. The plot-vs-mine comparison partly exists; the floor-area/footprint-vs-mine comparison is only half-wired and depends on trustworthy floor-area data. Treat "compare every property to the user's own home on size AND plot" as a required capability and assess how completely it's implemented.

**Every property gets a score /100** across weighted axes (Potential, Value, Fit, Edge, Permission) and a **typology** ("Development play", "Conversion play", "Setting play", etc.) plus a tier (High/Med/Low).

---

## Architecture (so you can navigate)

- **`run.py`** — the whole engine (single file, ~2300 lines, ~87 functions). Runs the nightly pipeline: gather leads → geocode → gate → measure → score → publish `docs/properties.json`.
- **`docs/index.html`** — single-file phone web app. Reads `properties.json`. Four-tab detail drawer (Overview / Land & Buildings / Planning & Value / Contact), shortlist, notes, manual-add, plot-overlay tool. Passcode-gated.
- **`.github/workflows/scout.yml`** — GitHub Actions: nightly cron (04:00) + manual dispatch. Passes `HOMEDATA_API_KEY` and `EPC_API_KEY` secrets to the run.
- **Local data files at repo root** (downloaded once, queried locally — see "the hard lesson" below):
  - `plots_waverley.json.gz` — HM Land Registry INSPIRE parcels (plot sizes).
  - `footprints_bowl.json.gz` — OS OpenMap Local building footprints.
  - `price_paid_region.json.gz` — HM Land Registry Price Paid sold prices (95k sales).
- **Converter scripts** (run locally, NOT deployed): `make_ppd.py`, `make_epc.py`.

Key functions to review: `fetch_probate_leads`, `fetch_listings`, `fetch_auction_lots`, `epc_lookup` + `_addr_match_score`, `find_comps` + `price_context_local`, `apply_gates`, `score_property`, `analyze_buildings` + `parcel_for`, `_fetch_notice_text` + `_probate_contact`.

---

## Data sources and their status

| Source | Gives | Access | Status |
|---|---|---|---|
| The Gazette | probate notices + executor/solicitor contact | keyless, OGL | works; notice pages need `Accept: text/html` + 10s crawl delay |
| EPC register (get-energy-performance-data.communities.gov.uk) | floor area, built form, property type | Bearer token (secret) | **working**; two-call search→certificate; built_form returns as NUMERIC CODES |
| HM Land Registry INSPIRE | plot boundaries | free download | local file — reliable |
| OS OpenMap Local | building footprints | free download | local file — reliable |
| HM Land Registry Price Paid | sold prices | free download | local file — reliable (live API 403-blocks cloud IPs) |
| planning.data.gov.uk | constraints (green belt, AONB, listed, flood) | keyless | live |
| PlanIt | planning approval history | keyless | live; rate-limits (429) |
| postcodes.io | postcode → coords | keyless | live |
| Homedata | live for-sale listings + EPC (paid) | key (secret) | monthly quota; now used ONLY for listings; enrichment moved to free EPC |

---

## The hard lessons (READ THIS before recommending changes)

These are decisions made deliberately after real failures. Don't "fix" them without understanding why:

1. **Live API calls from GitHub's servers are fragile.** They get rate-limited (429), IP-blocked (403 — cloud IPs are refused by several gov sites), and quota-capped. The sources that have NEVER failed are the ones downloaded once and stored as local files (INSPIRE, OS, Price Paid). **The strategic direction is: prefer local bulk data over live calls.** If you recommend a new live API, justify why it won't be blocked/throttled.

2. **A circuit-breaker exists (`_DEAD` set, `_kill`/`_dead`/`_throttled`).** The first 429/403 from any source parks it for the rest of the run, instead of hammering it hundreds of times. This protects my access from bans. Do not remove or weaken it. If anything, it should be applied to MORE sources.

3. **The Gazette has a published crawl policy** (1 request / 10 seconds, ideally 9pm–7am). The code obeys it (`GAZETTE_CRAWL_DELAY`). Respect this — a ban here kills the probate feed permanently.

4. **Postcode-centroid geocoding is imprecise.** Probate leads are located by postcode centroid, which often lands in the wrong (larger, enclosing) INSPIRE parcel — producing absurd plot/footprint figures (e.g. a "4,323 m² plot / 3,479 m² house" on a terraced house). Sanity gates now reject implausible matches (`MAX_PLAUSIBLE_PLOT_M2` etc.) and flag leads `approx-location`. This is a KNOWN accuracy ceiling; a real fix would need precise geocoding (UPRN → coordinate). **This is probably the single biggest remaining accuracy problem — scrutinise it.**

5. **EPC address matching is fuzzy** (`_addr_match_score`). Probate addresses ("Chompers, 4 Cedarways") don't cleanly map to EPC address lines. The matcher must NEVER attach a neighbour's certificate (that would give a house the wrong floor area / built form). It refuses to match across house-number ambiguity. Review this carefully — a wrong match silently corrupts type + floor area + valuation.

6. **EPC `built_form` comes back as a NUMERIC CODE** (1=Detached, 2=Semi-Detached, 3=End-Terrace, 4=Mid-Terrace...). This mapping was inferred, NOT found in authoritative docs, and verified only by spot-checking against Street View. **If the mapping is wrong, the tool silently drops the wrong properties.** Please try to find the authoritative RdSAP built-form enumeration and confirm it.

7. **No data beats bad data.** The tool deliberately drops/flags rather than display wrong numbers. Fewer, accurate leads > many misleading ones.

---

## The valuation approach (recently rebuilt — scrutinise)

The user's mental model is the human Zoopla process: *find comparable sold properties nearby, ideally same street, same type, similar size; use them to inform judgement.*

Current implementation (`find_comps`, `price_context_local`):
- **Same street first** (using Price Paid street names), then postcode, then sector, then district — widening only when thin.
- Same property type (from EPC built form).
- Recent sales (last ~12 years), each **restated to today's money** via a price index derived from the local data itself (median sale £/year — no external HPI feed).
- Estimate = median of chosen comps; band = their spread. Own-past-sale used as a strong anchor when available.
- **Named comparables surfaced to the app** so the user sees the evidence (address, sold price, year, today's value) and judges for themselves.

**Known limitation the user explicitly wants solved:** comps are not yet **size-matched**. The user wants "4 neighbouring properties that are ALSO 4-bed detached with XX sq ft." A scaffold exists (`find_comps` accepts `subject_fa`, `_load_epc_local`, `EPC_LOCAL_FILE`) that switches to **£/m² size-matched comps** automatically IF a bulk EPC file (`epc_region.json.gz`, built by `make_epc.py`) is present. It is currently absent. **Assess whether this approach is sound and whether the £/m² method is implemented correctly.** Note: bedroom count is unavailable in any free dataset; EPC habitable-rooms + floor area are the proxies.

---

## The vision / where this is going

**Ultimate goal:** a single tool that (a) ingests the ENTIRE public for-sale market AND a scraped off-market/pre-market layer, (b) scores everything through one consistent lens, (c) compares every property directly to my own home's size and plot, (d) prices accurately enough to trust six-figure decisions, and (e) lets me act (contact estate/agent) — so I find a forever home with development upside at a good price, ideally spotting it before the crowd.

The three headline demands, in the user's words: **drastically improve accuracy, scoring, and scouting (opportunity sourcing).** Weight your audit toward these three.

Priorities for the next phase (your recommendations should align to or challenge these):
1. **ACCURACY of the core numbers** — plot size, floor area, property type, and valuation must be trustworthy enough to act on. #1 concern. Recurring frustrations: "property" labels, wildly varying square footage, absurdly broad price ranges.
2. **SCORING quality** — does the score surface the genuinely good opportunities, across BOTH public and off-market stock, without being gamed by one (possibly mis-measured) axis? Is a Rightmove listing and a probate lead scored fairly against each other?
3. **SCOUTING / sourcing** — (a) comprehensive ingestion of the public for-sale market (currently weak — only Homedata), and (b) richer off-market signals (the moat). Both matter. Assess options and their ban/ToS/licensing risks.
4. **Compare-to-my-home** — size and plot vs the user's own, on every property, shown prominently. Required capability; assess completeness.
5. **Size-matched £/m² valuation** — once bulk EPC data is available (scaffold exists).
6. **Precise geocoding** — fix the postcode-centroid parcel-mismatch (biggest single accuracy bug).
7. **Listed-building / planning-constraint accuracy** — listed buildings are points; geocode must land close enough.
8. **Display / legibility** — four-tab layout is new; a tile-card redesign was deliberately deferred until data is trustworthy (pretty cards on bad data mislead).

**Explicitly NOT wanted:** turning this into a product/SaaS for others; features that show impressive-looking but WRONG data; scope creep beyond the personal property hunt. NOTE: broad public-listing ingestion IS wanted (see thesis) — but any method must weigh anti-bot/ToS/licensing/ban risk honestly; don't recommend naive portal scraping without addressing those risks.

---

## Constraints to respect in any recommendation

- **Runs free on GitHub Actions, phone-only user.** No paid infra, no local dev environment assumed. Deploys are done by uploading files via the GitHub web UI on a phone — so changes should be deployable that way.
- **Protect API access above all.** A throttle heals; a ban is fatal to a data source. Rate-limit discipline (circuit-breaker, crawl delays, one-run-a-day) is load-bearing.
- **Licensing:** local data is Open Government Licence / OS OpenData. Keep raw licensed data out of the public `docs/` folder — surface only derived facts. EPC address-level data is personal data (GDPR) — the tool is private/passcode-gated for this reason; don't expose it publicly.
- **Single-file engine and single-file frontend** are intentional (fewer moving parts, less to break, nothing that can be "turned off"). Don't recommend a framework rewrite unless the case is overwhelming.

---

## Good questions to pressure-test

- Is the `score_property` weighting actually surfacing the right properties, or is it gameable by one axis (e.g. a huge—possibly mis-measured—plot maxing Potential)?
- Does `apply_gates` drop things it shouldn't, or keep things it shouldn't?
- Is the parcel/footprint sanity-gating too loose or too tight?
- Are there silent-failure paths where bad data reaches the user unflagged?
- Is the circuit-breaker applied to every source that can throttle?
- Does the valuation mislead when comps are thin or size-unmatched?
- Is anything in `run.py` dead code, duplicated, or a latent crash (there was a history of both)?

Be objective and thorough. Challenge the design where the evidence supports it. The goal is a tool whose numbers I can trust enough to spend six figures acting on.
