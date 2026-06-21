"""
Central configuration. Everything you'll routinely tune lives here:
search criteria, scoring weights, and which adapters are switched on.

Turning a paid data source on later is a one-line change in ADAPTERS.
"""

# ---------------------------------------------------------------------------
# Search criteria — what the source adapters hunt for.
# ---------------------------------------------------------------------------
SEARCH = {
    "max_price": 800_000,
    "min_price": 450_000,
    "areas": ["GU10", "GU9", "GU35", "GU8", "GU27"],   # Farnham + surrounds
    "property_types": ["detached", "bungalow", "smallholding"],
    "min_beds": 2,
    "radius_of_station_mi": 12,            # Farnham station as the spine
    "station": {"name": "Farnham", "lat": 51.215, "lng": -0.802},
}

# ---------------------------------------------------------------------------
# Scoring weights — the maximum points each signal can contribute.
# The headline 0–100 score is the raw total normalised against MAX_TOTAL.
# Tune these as you learn what actually predicts a good buy for you.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "equity_residual": 20,
    "plot_size":       30,
    "structural":      25,
    "motivation":      20,
    "competition":     10,
    "location":        15,
}
MAX_TOTAL = sum(WEIGHTS.values())          # 120

# A property is "low competition" if it clears this competition score,
# or arrives via a structurally thin channel (auction / off-market).
LOW_COMP_THRESHOLD = 7
THIN_CHANNELS = {"auction", "off-market"}

# ---------------------------------------------------------------------------
# Renovation cost model — rough £/m² by EPC-implied condition.
# Used by the equity-residual calc. Starting heuristic; refine with quotes.
# ---------------------------------------------------------------------------
RENO_RATE_PER_M2 = {
    "poor":    1200,   # EPC F/G or pre-1950 unmodernised
    "dated":    900,   # EPC E/D
    "fair":     600,   # EPC C or better
}
EXTENSION_ALLOWANCE = 40_000   # added when structural signals suggest extending

# ---------------------------------------------------------------------------
# Adapter registry. The pipeline runs whatever is enabled, in order.
# Add a paid source later by importing it and appending one line — nothing
# downstream changes.
# ---------------------------------------------------------------------------
ADAPTERS = {
    "sources": [
        # (module path, class name, enabled)
        ("adapters.sources.homedata",  "HomedataSourceAdapter", True),   # paid API, no blocking
        ("adapters.sources.rightmove", "RightmoveAdapter",      False),  # free scraper (fallback)
        # ("adapters.sources.eig",     "EIGAuctionAdapter",     False),  # paid, auctions
    ],
    "enrichment": [
        ("adapters.enrichment.homedata",     "HomedataEnrichmentAdapter", True),
        # Land Registry stays ON even on the paid route: Homedata gives floor
        # area + comps + EPC, but NOT plot/land size — your #1 signal. INSPIRE
        # polygons (free) provide it.
        ("adapters.enrichment.landregistry", "LandRegistryAdapter", True),
        ("adapters.enrichment.epc",          "EPCAdapter",          False),  # Homedata covers EPC
    ],
}

# Secrets are read from environment variables (set as GitHub Action secrets).
# Never commit keys to the repo.
import os
HOMEDATA_API_KEY = os.environ.get("HOMEDATA_API_KEY", "")
EPC_API_KEY = os.environ.get("EPC_API_KEY", "")
EPC_API_EMAIL = os.environ.get("EPC_API_EMAIL", "")
