# PropertyScout

A personal property anomaly detector. It aggregates listings, scores them
against a composite signal model, computes the equity gain after renovation,
and surfaces the well-located doer-uppers and large plots that are off the
radar to most buyers.

Runs at £0 on free tiers. Architected so paid data sources plug in later as
drop-in adapters with no downstream changes.

---

## How it works

```
fetch (sources) → normalise → enrich → score → flag → store → publish
```

- Source adapters return listings in one common shape (scraper or paid API — same interface).
- Enrichment adapters attach EPC floor area, plot size, comps, schools, planning, constraints.
- The scorer runs six signals and a residual-value (equity) calc.
- Flags mark the search-invisibility reasons a property is hiding from the crowd.
- Results are stored in SQLite (committed to the repo, so price history and
  days-on-market accrue for free) and published to `docs/properties.json`.
- `docs/index.html` is the mobile-first frontend (map + list + shortlist),
  served by GitHub Pages.

---

## Run it locally

```bash
pip install -r requirements.txt
python run.py
open docs/index.html          # or serve docs/ on any static server
```

Out of the box every adapter runs in mock mode, so the pipeline produces a
real `properties.json` from sample data immediately. The frontend reads it.

---

## Going live

The repo now ships configured for the **Homedata route** (a licensed API — no
scraping, no IP blocking, runs fine from GitHub Actions):

- **Listings + market signals + EPC + comps:** Homedata. Set `HOMEDATA_API_KEY`,
  confirm the listings path in their playground, flip `USE_MOCK = False` in both
  `adapters/sources/homedata.py` and `adapters/enrichment/homedata.py`.
- **Plot / land size:** still the free Land Registry / INSPIRE lookup — Homedata
  does **not** provide plot size, and it's your top signal, so this stays on.

Two coverage boundaries worth knowing:
1. Homedata returns no free-text description, so probate/executor *keyword*
   detection won't fire from it. Motivation is instead driven by its structured
   reduction count, days-on-market and status chain — which is more reliable.
2. Area-wide listing search is a paid-plan feature; the free tier (100 calls/mo)
   is for testing + per-property enrichment. Budget the entry paid tier (~£49/mo)
   to actually populate the map daily. The runner only enriches new or
   price-changed listings, so credit use stays low.

### Free scraper route (alternative)

If you'd rather stay fully free, flip the adapters: enable `RightmoveAdapter`
plus the `EPCAdapter` and re-enable Land Registry, and disable the Homedata
pair. See each adapter's header for what to finish. Note the scraper runs from a
cloud IP and may be blocked by Rightmove without proxies.

## Going live (old notes)

---

## Adding a paid source later

The whole point of the adapter pattern. To add, say, an auction or
PropertyData feed:

1. Create `adapters/sources/yourfeed.py` with a class implementing
   `SourceAdapter.fetch() -> list[RawListing]`.
2. Add one line to `ADAPTERS["sources"]` in `config.py` with `enabled=True`.

Nothing else changes. Same for enrichment via `EnrichmentAdapter`.

---

## Tuning the model

`config.py` holds the search criteria, the scoring `WEIGHTS`, and the
renovation cost rates. The signal logic lives in `pipeline/score.py`.

The supplied heuristics are deliberately conservative — a starting point.
The real edge is calibrating them against your own accept/reject decisions
over time. Log why you pass on a property and feed that back into the weights.

---

## Deploy

1. Push to a GitHub repo.
2. Settings → Pages → serve from `/docs`.
3. Settings → Secrets → add `EPC_API_KEY`, `EPC_API_EMAIL`.
4. The Action in `.github/workflows/scout.yml` runs daily and commits the
   refreshed data. Trigger it manually from the Actions tab the first time.

---

## A note on scraping

Rightmove and Zoopla terms prohibit scraping. This project is for personal,
low-volume use. Scrape politely — slow rate, caching, off-peak — both to stay
within reasonable bounds and to avoid the blocks that would otherwise push you
toward paid proxies. The source adapters are the fragile layer by design: when
one breaks, swap in a paid listing API as a replacement adapter.
