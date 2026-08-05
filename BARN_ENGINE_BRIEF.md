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

- **TASK A — Rebuild footprints WITH outlines** *(in progress — this is where we
  are starting)*. A converter that produces building **polygons** matching the
  parcels schema `{"a", "b":[latmin,latmax,lonmin,lonmax], "r":[[lat,lon],…]}`,
  area computed in British National Grid, MultiPolygons handled explicitly
  (never concatenate rings), clipped to `AREA_POLYGON`, with a loud data-quality
  report. Delivered as a **workflow button** (phone-only). Unlocks the shape &
  elongation signals. *(Source route being verified before coding — see Open
  questions.)*
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

- **What is the current access route for building-outline polygons** (OS
  OpenMap Local vs OSM/Geofabrik vs Overpass)? Needs a key? Direct URL? Cloud-IP
  friendly? Under 25 MB? *(Being researched now — decides TASK A's source.)*
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
| Aug 2026 | Brief written. Reframe + fact-check + plan agreed. Starting TASK A (footprints rebuild). Source route under research. On-market "planning moat" (the listings side) already shipped separately. |

*Append a row whenever a task lands or a fact changes.*
