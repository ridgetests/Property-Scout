# PropertyScout

A personal property-hunting radar for one user. It scans the public for-sale
market **and** an off-market/pre-market edge (probate estates, auctions), scores
everything through one lens, compares each property to my own home's size and
plot, and surfaces the well-located doer-uppers and large plots with development
upside before the crowd sees them.

It runs itself for free on GitHub Actions (nightly) and publishes a passcode-gated
web page I open on my phone. Not a product — a single-purpose tool for my own hunt.

---

## Architecture

Two files do the work, by design (fewer moving parts, nothing that can be
"turned off"):

- **`run.py`** — the whole engine. One file, standard library + `requests`.
  Runs the nightly pipeline end to end.
- **`docs/index.html`** — the single-file phone web app (map + list + four-tab
  detail drawer + shortlist + notes + manual-add + plot-overlay). Reads
  `docs/properties.json`. Passcode-gated.
- **`.github/workflows/scout.yml`** — nightly cron (04:00) + manual dispatch.
  Runs `run.py`, commits `data/scout.db` + `docs/properties.json`, deploys `docs/`
  to GitHub Pages.

### The pipeline

```
gather → geocode → gate → measure → score → publish
```

- **Gather** — probate/deceased-estate notices (The Gazette), auction lots
  (Clive Emson, Auction House), and live for-sale listings (Homedata).
- **Geocode** — postcode → coordinate (postcodes.io). Optionally snapped to the
  precise **UPRN** coordinate when available (see below), which fixes the
  wrong-parcel plot problem.
- **Gate** — inside the hand-drawn target polygon, detached-only, plot ≥ home plot,
  care-home / out-of-area exclusions.
- **Measure** — plot size (HM Land Registry INSPIRE parcels), building footprints
  (OS OpenMap Local), floor area + built form + energy rating (free EPC register),
  comparable sold prices (HM Land Registry Price Paid).
- **Score** — a /100 composite across **Potential, Value, Fit, Edge** and a
  **Feasibility** multiplier (planning constraints + local approval precedent),
  plus a typology ("Development play", "Conversion play", "Setting play"…) and a
  High/Med/Low tier. The displayed breakdown reconciles with the headline score.
- **Publish** — `docs/properties.json` (personal data stripped, see Privacy).

---

## Local bulk data files ("download once, query locally")

Live API calls from cloud IPs get rate-limited, IP-blocked and quota-capped. The
sources that have never failed are the ones downloaded once and stored as local
files, queried offline. `run.py` reads these from the repo root:

| File | Contains | Source (all free) | Required? |
|---|---|---|---|
| `plots_waverley.json.gz` | INSPIRE parcel boundaries (plot sizes) | HM Land Registry INSPIRE | yes |
| `footprints_bowl.json.gz` | building footprints | OS OpenMap Local | yes |
| `price_paid_region.json.gz` | sold prices (~95k sales) | HM Land Registry Price Paid | yes |
| `epc_region.json.gz` | bulk EPC certs for size-matched comps | EPC register | optional |
| `uprn_coords.json.gz` | UPRN → precise coordinate | OS Open UPRN | optional |

The optional files activate features when present and are a clean no-op when
absent (comps fall back to area bands; geocoding falls back to postcode centroids).

### Converters (run locally, not on Actions)

`make_uprn.py` builds `uprn_coords.json.gz` from the free OS Open UPRN CSV — see
its header for usage, and "Precise geocoding" below. The Price Paid and bulk-EPC
files are built by equivalent local converters (`make_ppd.py` / `make_epc.py`)
kept outside the deploy path; only their outputs are committed.

### Precise geocoding (optional — fixes the plot-mismatch bug)

Postcode-centroid geocoding often lands in the wrong (enclosing) parcel and
reports a whole estate/field as the plot. When `run.py` resolves a property's
UPRN (from its EPC certificate or a portal feed) it snaps the property onto the
exact coordinate, so the plot/footprint match is the dwelling's own and the
`approx-location` flag is dropped. To enable:

1. Download **OS Open UPRN** (CSV), free under the Open Government Licence:
   <https://osdatahub.os.uk/downloads/open/OpenUPRN>
2. Locally: `python make_uprn.py osopenuprn_<date>.csv` → `uprn_coords.json.gz`
3. Upload `uprn_coords.json.gz` to the repo root via the GitHub web UI.

---

## Run it locally

```bash
pip install -r requirements.txt
python run.py                    # writes docs/properties.json
open docs/index.html             # or serve docs/ on any static server
```

Set `USE_MOCK = True` in `run.py` to run without API keys against sample data.

---

## Secrets (GitHub → Settings → Secrets)

- `HOMEDATA_API_KEY` — live for-sale listings (licensed API; no scraping).
- `EPC_API_KEY` — free government EPC register (floor area, built form, rating).

Both are passed to the run by `scout.yml`. The tool works without them (auctions +
probate + local files still run), just with thinner public-listing coverage.

---

## Deploy (phone-friendly)

Changes are deployed by uploading files via the GitHub web UI. The Action runs
nightly and on manual dispatch (Actions tab), commits the refreshed
`data/scout.db` (so price history and days-on-market accrue) and
`docs/properties.json`, and deploys `docs/` to Pages.

---

## Design constraints (load-bearing — read before changing)

- **Protect API access above all.** A throttle heals; a ban is fatal. A
  circuit-breaker (`_DEAD` / `_kill`) parks any source on its first 429/403 for the
  rest of the run; the Gazette crawl delay (1 req / 10s) is obeyed; live results
  are cached in SQLite so a source is hit at most once per point. Prefer local bulk
  data over live calls.
- **No data beats bad data.** The tool drops or flags rather than show wrong
  numbers (implausible plots rejected, EPC never matched to a neighbour, unverified
  plots flagged `approx-location`).
- **Privacy.** The site runs on free GitHub Pages, so the repo — and therefore both
  `docs/properties.json` and the committed `data/scout.db` — is public. Person-level
  data (deceased names, executor/solicitor contacts, notice text, precise
  identifiers) is stripped from everything committed; the Gazette notice link is
  kept so contacts are reached on demand from the already-public statutory notice.
  The passcode gate is client-side only — treat it as cosmetic, not access control.
  A private-hosting move is the real fix.

---

## Tuning

Everything tunable lives at the top of `run.py`: the search box (`SEARCH`,
`AREA_POLYGON`), your home anchor (`HOME`, `HOME_FLOOR_AREA_M2`, `HOME_PLOT_M2`),
the score composition (`score_property`), and the plot-sanity thresholds
(`MAX_PLAUSIBLE_*`). The real edge is calibrating these against your own
accept/reject decisions over time.
