# PropertyScout — The Latent Opportunity Engine (barns / Class Q): master brief

*One place to come back to. This reconciles the three source documents
(`Spec_Latent_Opportunity_Engine.md`, `Spec_Aerial_Scanner.md`,
`CLAUDE_CODE_BRIEF_AerialScanner.md`) with a fact-check against the real repo,
and records the plan, the decisions, and the progress. If those documents and
this one ever disagree, **this one wins** — the others are older.*

*Started: August 2026. Keep the Progress log (bottom) updated as work lands.*

---

## 1. The idea, in one paragraph

The only property with **zero competition** is the one that isn't for sale at
all. The most addressable version: **redundant farm buildings** that can become
homes under **Class Q permitted development** — a legal *right*, not a planning
gamble. A barn with Class Q potential is worth a multiple of one without it, and
the farmer often doesn't know, can't fund it, or won't bother. We find these,
work out what each is worth to us, and approach the owner directly. It runs
almost entirely on Ordnance Survey + planning data **already in the repo**.

This is the OFF-market mirror of the on-market "planning moat" already shipped
(which reads Class Q language in live listings). Same idea, opposite side of the
market.

---

## 2. The one reframe that decides the whole approach

> **Detect from vectors → classify from height/context → verify with imagery.**

- **Detect** — you do NOT need satellite pictures to find buildings. Ordnance
  Survey already mapped every building in England as a polygon. That's the
  "scan from above," already done, for free.
- **Classify** (barn vs house) — from **shape** (barns are big simple
  rectangles), **height** (barns are low for their size — LIDAR), and
  **context** (field-scale parcel, isolated, road access, farmland).
- **Verify** — imagery, for the ~100–200 shortlisted candidates only, **by
  human eye**. Never machine-scan imagery.

This is how a weather scanner or a targeting system works: sweep cheaply on
signature, confirm expensively on the few that trip the filter.

### Google Earth / Google Maps — the verdict
- **Machine-scanning their imagery to find/classify barns: NO.** It breaks their
  terms (no bulk download, caching, or automated analysis to build a dataset),
  which risks an **API ban** — the one outcome we most avoid. It's also the
  expensive, less-accurate way to solve a problem free vector data already
  solves.
- **As a click-out link for a human to eyeball a candidate: YES**, fine.
- **Google Earth *Engine*** (their cloud geospatial compute): technically a fit
  and it avoids downloading huge files — BUT it needs a Google account + a
  service-account key to automate (hard from a phone), and its free tier is
  *non-commercial* (grey area for property development). Treat as a **fallback**
  if the direct LIDAR download blocks GitHub's IP, not the default.
- For any **saved/committed** thumbnail use **ArcGIS World Imagery** (already in
  the app, keyless, no ToS issue). Google/Bing = links only, never cached.

---

## 3. The data stack (cheapest / most-legal first)

| Layer | Answers | Source | Status |
|---|---|---|---|
| Building footprints | *where* every building is, size | OS OpenMap Local | **have area+centre only — needs rebuild WITH outlines** |
| Parcels | field vs garden | HMLR INSPIRE | have ✓ (has rings) |
| Shape / elongation | simple rectangle? long+thin? | derived from outlines | **blocked on the footprint rebuild** |
| Height / 3D ⭐ | barn vs house (the killer signal) | EA LIDAR (free, OGL) | not started; download is fiddly + cloud-IP risk |
| Use tags | "barn / farmyard" already labelled | OpenStreetMap (ODbL) | not started |
| Crop map | is the land genuinely farmed? | CROME (RPA, free) | not started |
| Roads | Class Q needs highway access | OS Open Roads (OGL) | not started |
| Planning history ⭐ | who already applied for Class Q | PlanIt / planning.data.gov.uk | integrated (rate-limited) |
| Imagery | human eyeball of the shortlist | ArcGIS / Google-link | app already has aerial thumbs |

⭐ = highest value.

---

## 4. Fact-check — where the source specs are wrong or optimistic

Recorded so we don't re-trip these:

1. **The repo does NOT hold building outlines.** `footprints_bowl.json.gz` is
   `{"a": area_m2, "c": [lat,lon]}` — area + a centre point, **no polygon**. So
   the "shape simplicity" and "elongation" signals the specs mark as *already
   have* are **not available until the footprints file is rebuilt with rings.**
   That rebuild is the true first blocker (this document's TASK A).
2. **Footprints file has data-quality faults:** ~9.3% zero-area records, and 707
   records over 20,000 m² (largest 581 hectares — impossible for a building,
   likely a merged MultiPolygon). The rebuild must fix these and **fail loudly
   if >1% are zero-area.**
3. **LIDAR is not "just download GeoTIFF tiles, no account."** Use the **LIDAR
   Composite DTM/DSM 1m** (NOT the National LIDAR Programme, whose tiles you
   can't name from a grid ref). The real portal is an interactive map app, and
   it **may refuse GitHub's data-centre IP** — so it needs a **probe first**,
   behind the circuit-breaker.
4. **Google Earth Engine asset ID** cited in the spec
   (`UK/EA/ENGLAND_1M_TERRAIN/2022`) is **unverified** — confirm before use.
5. **Shortlist size is ~190 (score ≥0.7), not "~50".** Plan to hand-review
   hundreds.
6. **"Offline one-off analysis"** — for a phone-only user that means a **GitHub
   Actions workflow button** (`workflow_dispatch`), not a laptop run.
7. **Google Static Maps** is fine to *show* on demand but **must not be cached /
   committed** into the public repo (breaches Google caching terms + our own
   "no licensed data in `docs/`" rule). Use ArcGIS for anything saved.

---

## 5. The hard gates (correctness, not features)

- **Designation is binary for Class Q.** Excluded on **National Landscapes
  (AONBs), Conservation Areas, National Parks, listed buildings, SSSIs,
  scheduled monuments**, and where an **Article 4** direction removes the right.
  A barn inside any of these is **not a Class Q play** — hard-filter, never
  soft-penalise. **Green Belt is FINE** (common misconception) — much of the
  Farnham countryside is Green Belt and stays eligible.
- **🚨 Surrey Hills National Landscape is being EXTENDED, over this exact bowl**
  (draft maps cover Frensham, Dockenfield, Rowledge, the Wey Valley). If the
  Order is confirmed, Class Q dies across the new land overnight. So: (a) gate on
  the **live** `area-of-outstanding-natural-beauty` dataset every run and assert
  it still excludes the extension; (b) add a separate **"inside draft extension —
  Class Q at risk"** flag; (c) this must be re-checked before any real decision.
  As last verified (early 2026) the Order was **not yet confirmed** — do not
  assume it hasn't moved.
- **Class Q is per agricultural unit, not per building** (max 10 dwellings /
  1,000 m² total / 150 m² per dwelling). Titles can't be split to game it.
- **Hibbitt / structural test:** it must be a *conversion*, not a *rebuild*. The
  most convertible barns are ugly modern steel-portal sheds, not picturesque
  frail timber ones. We can't judge structure from data — imagery + a survey do
  that. Surface candidates; don't claim they're convertible.

---

## 6. Build order (the plan)

Cheapest / warmest-lead first. **Steps A–D need no imagery, no LIDAR, no new
accounts** — mostly data already in the repo.

- **TASK A — Build footprints WITH outlines** *(converter written; awaiting first
  workflow run)*. `make_footprints.py` produces building **polygons** matching
  the parcels schema `{"a", "b":[latmin,latmax,lonmin,lonmax], "r":[[lat,lon],…],
  "c":[lat,lon], "u":usetag}`, area in British National Grid, MultiPolygons →
  largest part (never concatenate rings), clipped to `AREA_POLYGON`, loud
  data-quality report, **fails without writing** if the result looks wrong.
  Delivered as the **Build barn footprints** workflow button (phone-only).
  Unlocks the shape & elongation signals.
  - **Source decision: OpenStreetMap** (via Geofabrik Surrey + Hampshire `.osm.pbf`,
    parsed with `pyrosm`), NOT OS OpenMap Local. Reason: OSM is the only free
    source that also carries **building-use tags** (`building=barn` /
    `farm_auxiliary` / `stable`, `landuse=farmyard`) — a building already tagged
    "barn" is a near-certain barn, no shape/height guess needed. Licence ODbL
    (share-alike; surface derived facts only). Trade-off: OSM rural-outbuilding
    completeness is good-but-not-100% vs OS's authoritative coverage — if it
    proves patchy, cross-fill geometry from Microsoft's UK footprints (keyless,
    but no use-tags). Farnham straddles the county line, so both counties are read
    and deduped.
  - **Written to a SEPARATE file** (`building_polygons.json.gz`), NOT over the
    existing `footprints_bowl.json.gz`: the core tool sizes listings from the OS
    footprints, and silently swapping OS→OSM there could regress that. The barn
    engine stays decoupled from the live tool; unify later only if warranted.
- **TASK B — Class Q planning-application mining.** Query planning data for
  agricultural-conversion / prior-approval applications in the bowl; classify
  approved / refused / pending; flag **"approved but not implemented"** (the
  hottest lead) and **"refused on a fixable point"** (cheap, unlockable). Uses
  PlanIt / planning.data.gov.uk already integrated. *Cheapest, highest signal —
  can run in parallel with A.*
- **TASK C — Designation hard-gate + Surrey Hills draft-extension flag** (§5).
- **TASK D — `scan_barns()` folded into `run.py`** as a "Barn play" lead type,
  reusing the existing parcel/footprint/point-in-polygon helpers and the grid
  index from the `scan_barns.py` prototype. Scored through the same pipeline;
  Value axis uses **residual land value** (there's no asking price).
- **TASK E — LIDAR heights** (the real "eye in the sky"). Probe one tile first
  (loud diagnostics, circuit-breaker); if Actions is allowed, fetch the bowl's
  Composite DTM+DSM, compute normalised height per footprint, emit
  `building_heights.json.gz` (`{id, ridge_m, mean_m, height_variance}` only —
  never commit raster). `workflow_dispatch` only, never the nightly cron.
- **TASK F — Deal sheet + verification.** Per candidate: convertible floorspace
  under the 10/1,000/150 rules, designation pass/fail, residual land value from
  the comps engine minus a barn-conversion £/m² cost, and an ArcGIS aerial +
  Google-Earth link for the human check.
- **Later:** precedent heat-map (which parishes approve Class Q), the
  "just-failed" list, plot-split detection on over-large gardens, Sentinel-2
  "farm-stress" NDVI hint (clever but noisy — a soft prioritiser, not evidence).

---

## 7. Constraints that never change (from the main brief + LEARNINGS.md)

- **Protect API access above all. A throttle heals; a ban is fatal.** Every new
  source goes behind the circuit-breaker (`_dead`/`_kill`/`_throttled`).
- **Prefer download-once, query-a-local-file** over live calls. Gov/cloud sites
  block data-centre IPs (HMLR live API 403s; the EPC bulk service moved behind a
  login). Verify a source is reachable *from Actions* before building on it.
- **Heavy work is `workflow_dispatch` only — never the nightly cron.** One full
  scout run per day.
- **Phone-only user, deploys via the GitHub web UI.** Converters need a
  **button** or an upload flow, not "run it locally." Web upload caps at ~25 MB.
- **Licensing:** OGL/OS OpenData is fine to commit as *derived facts*; keep raw
  licensed rasters out of `docs/`. EPC/address-level data is personal (GDPR) —
  the app is private/passcode-gated for that reason. OSM is **ODbL /
  share-alike** — check compatibility before relying on derived OSM facts.
- **No data beats bad data.** Drop/flag rather than show wrong numbers.

---

## 8. Open questions (verify — don't guess)

- ~~What is the current access route for building-outline polygons?~~
  **RESOLVED:** OSM via Geofabrik county `.osm.pbf` (keyless, direct URL,
  cloud-friendly, carries use-tags), parsed with `pyrosm`. *Still unverified:
  exact county-extract byte size and whether Geofabrik serves GitHub's IP — the
  workflow probes this on first run and fails loudly if not.*
- Does `environment.data.gov.uk` serve LIDAR to a GitHub Actions IP? (Probe.)
- Is the GEE `UK/EA/ENGLAND_1M_TERRAIN/2022` asset ID correct?
- Does OSM's ODbL share-alike clash with how we surface derived facts?
- Does OS Open Zoomstack / NGD carry building-USE attribution that would make the
  whole height-classification step unnecessary? (Worth 10 minutes before LIDAR.)
- Current status of the Surrey Hills extension Order (re-check before acting).

---

## 9. Progress log

| Date | What happened |
|---|---|
| Aug 2026 | Brief written. Reframe + fact-check + plan agreed. On-market "planning moat" (listings side) already shipped separately. |
| Aug 2026 | TASK A: source resolved to **OSM/Geofabrik** (use-tags bonus). `make_footprints.py` + **Build barn footprints** workflow written; writes `building_polygons.json.gz` (separate from the OS footprints). Awaiting first workflow run to verify Geofabrik download from Actions + real building count. |
| Aug 2026 | TASK A **DONE**: first run built `building_polygons.json.gz` — **53,580 buildings**, 2.9 MB, **0 zero-area / 0 giants** (old file had 9.3% / 707), Geofabrik download worked from Actions. |
| Aug 2026 | TASK C+D: `make_barn_candidates.py` + **Build barn candidates** workflow. Geometry funnel (offline, real data): 53,580 → 8,065 size-band → 1,175 on-field/agri-tagged → **59 at score ≥0.7, 28 ≥0.8**. Designation HARD-GATE wired via `run.fetch_constraints` (extended with National Park + SSSI), Green Belt correctly kept eligible. Writes `barn_candidates.json` (ranked, gated). Awaiting first designation run. |
| Aug 2026 | TASK C+D **DONE**: first designation run checked top 200 → **95 Class-Q eligible** / 105 excluded (mostly AONB, clustered east; eligible ones cluster west, away from Surrey Hills). One transient 502 handled (breaker untripped); failed fetches now marked "not checked", not eligible. |
| Aug 2026 | **Viewer shipped** (`docs/barns.html`): Leaflet + Esri satellite map, a pin per candidate (green=eligible / red=excluded / grey=unchecked), popups with size, dwellings, parcel, eligibility + Google Maps/Earth links. Reuses the app passcode; linked from the main app header (🏚). Data moved to `docs/barn_candidates.json`. |
| Aug 2026 | TASK B: Class-Q **planning mining** (`make_barn_planning.py` + **Build barn planning leads** workflow). PlanIt API confirmed via research: `bbox` (lng-first) + free-text `search` (no Class-Q app_type exists), classify by `app_state`, no built/commencement data (dormancy inferred from `decided_date` age). Mines the bowl in ~4 spaced (10s) requests, behind the breaker. Flags **approved-but-dormant** (1-3 yr, hottest), **approved-lapsed** (>3 yr), **refused** (maybe fixable). Writes `docs/barn_planning.json`; barns map shows them as a toggleable gold/purple pin layer. Awaiting first mine run. |
| Aug 2026 | First mine run **400'd** on every call (unsupported param — `select`/`sort`, which the research couldn't verify). Fix: dropped `select`/`sort`; the miner now **self-discovers** a working parameter shape (fullest bbox query → simpler → `lat/lng/krad` fallback that run.py already uses), so one run finds what PlanIt accepts. No rate-limit was hit (400 ≠ 429). Awaiting re-run. |
| Aug 2026 | Mine **WORKED**: **117 Class-Q applications** — 12 approved-dormant (real named farms: Binton, Groomes, Oaklea…), 45 lapsed, 31 refused. Cleanups: miner now drops council-office addresses (LPA noise). Barns map gained a 🏠 home marker + dashed search-area outline for orientation. |
| Aug 2026 | Care-home false positive fixed (main tool): a probate lead "43 Waverley Lane" was Waverley Grange Care Home, surfaced as a dev play because the notice gave only the street address (no "care home" text). Added a `KNOWN_CAREHOME_PCS` postcode guard (probate build + gate). **Roadmap: CQC care-home register** as the authoritative fix (download-once local file). |
| Aug 2026 | TASK: **CQC care-home register** DONE. `make_carehomes.py` + **Build care-home list** workflow downloads CQC `HSCA_Active_Locations.ods` (keyless, `Care home? == Y`), filters to the area districts, writes `care_homes_region.json.gz`. run.py `_load_carehomes()` merges it with the seed; `_is_known_carehome` now catches *every* registered care home automatically. (Research: ODS not xlsx → pandas+odfpy; browser UA needed; monthly filename → scrape page + constructed-URL fallback.) |
| Aug 2026 | TASK: **Deal-sheets** DONE. `make_barn_candidates.py` computes a local mid-size detached £/m² (Price Paid × EPC, recent) and attaches a rough residual-value **opening-offer range** per candidate (end value − conversion cost − developer margin, per Class-Q dwelling). Shown in the barns-map popups; assumptions (£/m², £2,200/m² build, 20% margin, 0.9 rural discount) surfaced in the output. Loudly labelled indicative triage, not a valuation. |
| Aug 2026 | TASK E: **LIDAR probe** (3 iterations). Findings: catalogue search works from Actions (NOT IP-blocked); the `/tiles/...` download is auth-gated (401); the **WCS route works** — `GetCoverage` with native BNG subsets `E(...)`/`N(...)`, `format=image/tiff` returned a real GeoTIFF. Coverage id `<uuid>__Lidar_Composite_Elevation_DTM_1m` (Elevation, not Hillshade). |
| Aug 2026 | TASK E **DONE**: `make_building_heights.py` + **Build building heights** workflow. Per eligible candidate, WCS GetCoverage DTM + first-return DSM, nDSM = DSM − DTM, records ridge/mean height → `docs/building_heights.json`. Barns map shows the height + a **"Hide house-like (LIDAR)"** toggle. ~2 spaced WCS requests per eligible candidate, behind a breaker; never commits raster. **Pipeline verified working from Actions** (rasterio reads the WCS tiffs). |
| Aug 2026 | Height v1 flagged 53/95 "tall" — a **box catching nearby trees** (first-return LIDAR sees canopy; the box had an 8 m margin). Fix: sample the **building OUTLINE** (rasterize the polygon from `building_polygons.json.gz`, all 95 matched) instead of a box, and use the **95th percentile** (robust to a stray tall pixel) — so height reflects the roof, not adjacent trees. Re-run pending. |
| Aug 2026 | **Disused/derelict** expansion. `make_footprints.py` now captures OSM lifecycle tags (`disused:building`, `abandoned`, `ruins`, `historic=ruins`, `building=ruins/collapsed`, `building:condition=derelict`) into a `d` flag. `make_barn_candidates.py` treats derelict buildings as prime candidates: size floor relaxed to 100 m² for agri/derelict, kept even off-field, +0.15 score uplift, `tier="derelict"`. Barns map shows a "⚠ disused/derelict" badge. Needs a re-run of **Build barn footprints** then **Build barn candidates**. (Note: this finds MAPPED derelict buildings; genuinely unmapped/tree-hidden ones need LIDAR anomaly detection — a separate future build.) |

*Append a row whenever a task lands or a fact changes.*
