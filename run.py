#!/usr/bin/env python3
"""
PropertyScout - single-file runner (Homedata live-listings, free tier).

One file, no package folders. Only external dependency: requests.
Set USE_MOCK = False for live data.

Free-tier reality (discovered from the live response): live listings give
street + postcode + price + beds + type + motivation signals (reductions,
days-on-market), but NO uprn, NO coordinates, NO description. So:
  - addresses come from street + postcode
  - map pins come from geocoding the postcode (postcodes.io, free)
  - equity / plot need the paid per-property reveal, so they're parked
  - scoring normalises against whatever data is present
"""
from __future__ import annotations
import os, re, json, gzip, sqlite3, hashlib, time, math, html
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

# ===========================================================================
# CONFIG
# ===========================================================================
USE_MOCK = False

SEARCH = {
    "max_price": 800_000,
    "min_price": 450_000,
    "areas": ["Waverley", "East Hampshire"],   # boundary names
    "min_beds": 2,
}
# Property types worth keeping (substring match, lowercase). Semis/terraces/
# flats/new-builds are filtered out as off-thesis.
TARGET_TYPES = ("detached", "bungalow", "cottage", "farm",
                "smallholding", "land", "plot", "equestrian")

# Home anchor (Wrecclesham) and the hand-drawn target patch: the rural bowl
# south of Farnham. Properties outside this polygon are dropped. Vertices are
# (lat, lng), tracing south-of-Farnham -> Tilford -> Churt -> Rowledge.
HOME = (51.198, -0.832)
AREA_POLYGON = [
    (51.268, -0.826), (51.248, -0.792), (51.236, -0.752), (51.232, -0.700), (51.222, -0.664),
    (51.196, -0.646), (51.186, -0.632), (51.160, -0.660), (51.143, -0.716), (51.148, -0.760),
    (51.138, -0.802), (51.132, -0.812), (51.160, -0.858), (51.192, -0.845), (51.212, -0.836),
    (51.224, -0.816), (51.240, -0.875), (51.255, -0.884),
]

AGENTS_ENABLED = True   # local estate-agent website listings (fetch_agents.py) - the
                        # portal-free "Rightmove replacement". Fully guarded: any failure
                        # or an unreachable site can never break the nightly run.
AUCTION_ENABLED = True
CLIVE_EMSON_URL = "https://www.cliveemson.co.uk/properties/"
AUCTION_HOUSE_URL = "https://www.auctionhouse.co.uk/sussexandhampshire/auction/search-results"
AUCTION_SKIP_TYPES = ("apartment", "flat", "maisonette", "garage", "commercial")
AUCTION_RADIUS_MI = 20  # auction stock is rarer, so cast a wider net than the polygon
PROBATE_ENABLED = True
PROBATE_LOCATION = "Farnham"   # Gazette accepts a town or full postcode
PROBATE_RADIUS_MI = 8          # probate is about your actual patch - keep it tight
PROBATE_LOOKBACK_DAYS = 120    # estates can take months to reach the market
# --- only surface probate leads that fit your buying criteria ---
PROBATE_MAX_PRICE = 900_000              # skip estimates above this
PROBATE_MIN_PRICE = 300_000              # skip cheap flats / small terraces
PROBATE_TYPES = ("detached", "semi", "terraced")   # houses only (excludes "flat"); widen/narrow freely
PROBATE_KEEP_UNKNOWN = True              # keep long-held homes Land Registry can't price (often the best)
HOME_FLOOR_AREA_M2 = 144                 # 3 Boundstone Close internal floor area (m2), from floor plan
HOME_FOOTPRINT_M2 = 72                   # home ground-floor footprint (m2) = 144 floor / 2 storeys. The
                                         # home is a SEMI, so its OS footprint is merged with the neighbour
                                         # and can't be read directly - this is the derived baseline.
HOME_PLOT_M2 = 380                       # measured plot area (m2) from title plan SY519861
HOME_PLOT_OUTLINE = [[-5.27, -19.32], [4.58, -19.32], [6.07, 19.32], [-5.38, 19.32]]  # plot shape, centred metres
PLOTS_FILE = Path(__file__).resolve().parent / "plots_waverley.json.gz"  # HMLR INSPIRE parcels
FOOTPRINTS_FILE = Path(__file__).resolve().parent / "footprints_bowl.json.gz"  # OS OpenMap Local buildings
PPD_FILE = Path(__file__).resolve().parent / "price_paid_region.json.gz"  # HM Land Registry Price Paid (local)
EPC_LOCAL_FILE = Path(__file__).resolve().parent / "epc_region.json.gz"  # bulk EPC certs for comps (optional)
UPRN_FILE = Path(__file__).resolve().parent / "uprn_coords.json.gz"  # OS Open UPRN -> precise coords (optional; built by make_uprn.py)
MIN_PLOT_M2 = HOME_PLOT_M2               # gate: lead plot must be >= your home plot
DETACHED_ONLY = True                     # gate: drop clear non-detached dwellings
EXCLUDE_DISTRICTS = {"GU11", "GU12", "GU14", "GU51", "GU52"}  # Aldershot/Farnborough/Fleet - out of area
EXCLUDE_LOCALITIES = {"Hale", "Badshot Lea", "Heath End", "Weybourne"}  # out-of-area GU9/GU10 sprawl

_CAREHOME_RE = re.compile(
    r"(nursing home|care home|rest home|residential home|retirement home|"
    r"residential care|convalescent|care centre|nursing centre|nursing home|"
    r"\bnursing\b|\bcare of\b|\bc/o\b|hospice|almshouse|"
    r"sheltered housing|extra care|assisted living)", re.I)
_LOCALITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(x) for x in EXCLUDE_LOCALITIES) + r")\b", re.I)


def _is_carehome(text):
    return bool(_CAREHOME_RE.search(text or ""))


def _is_excluded_locality(text):
    return bool(_LOCALITY_RE.search(text or ""))
LISTING_EPC_CAP = 30                     # max subject EPC lookups for public listings per run (cached forever)
NOTICE_DETAIL_CAP = 15                   # max per-notice page fetches per run (politeness cap)
GAZETTE_CRAWL_DELAY = 10                  # seconds between Gazette requests (their published crawl-delay)
_UA = "Mozilla/5.0 (compatible; PropertyScout/1.0)"

# --- throttle circuit-breaker: first 429 from a source parks it for the rest of
# the run, so one rate-limit never snowballs into hundreds of hammering retries. ---
_DEAD = set()


def _dead(api):
    return api in _DEAD


def _kill(api, why=""):
    if api not in _DEAD:
        _DEAD.add(api)
        print(f"   \u26a0 backing off {api} for the rest of this run ({why}) - protects the quota")


def _throttled(e):
    """429 = rate-limited, 403 = blocked outright (e.g. cloud IPs refused).
    Either way: stop calling that source for the rest of this run."""
    t = str(e)
    return ("429" in t or "Too Many Requests" in t
            or "403" in t or "Forbidden" in t)

WEIGHTS = {"equity_residual": 20, "plot_size": 30, "structural": 25,
           "motivation": 20, "competition": 10, "location": 15}
RENO_RATE_PER_M2 = {"poor": 1200, "dated": 900, "fair": 600}
EXTENSION_ALLOWANCE = 40_000
LOW_COMP_THRESHOLD = 7

HOMEDATA_API_KEY = os.environ.get("HOMEDATA_API_KEY", "")
EPC_API_KEY = os.environ.get("EPC_API_KEY", "")          # free govt EPC register (GitHub secret)
EPC_BASE = "https://api.get-energy-performance-data.communities.gov.uk"
EPC_ENABLED = True
# Plot-sanity: a residential plot bigger than this is almost certainly a mis-matched
# enclosing parcel (estate/field), not the property's own plot. Reject rather than
# publish garbage (this is what produced a "4,323 m2 plot / 3,479 m2 house" on a semi).
MAX_PLAUSIBLE_PLOT_M2 = 4000   # ~1 acre; bigger = almost certainly a mis-matched estate/field parcel
MAX_PLAUSIBLE_MAIN_M2 = 600     # a single dwelling footprint above this = merged/estate polygon
MAX_PLAUSIBLE_COVERAGE = 60     # % of plot built on; above this the parcel match is suspect
HOMEDATA_BASE = os.environ.get("HOMEDATA_BASE", "https://api.homedata.co.uk")
ENRICH = False      # Homedata per-property enrichment (epc-checker + comparables). MUST
                    # stay False: Homedata is reserved for live listings only, and its
                    # ~100/month quota is burned fast by enrichment. Free EPC + local
                    # Price Paid do the enrichment now. Do not flip without a quota plan.
ENRICH_TOP_N = 25

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "scout.db"
OUT_PATH = ROOT / "docs" / "properties.json"

STRUCTURAL_TERMS = ["planning permission", "development potential", "scope to",
                    "potential to", "annexe", "outbuilding", "workshop", "barn",
                    "in need of modernisation", "modernisation", "renovation"]
MOTIVATION_TERMS = ["executor", "probate", "estate of", "sold as seen",
                    "no onward chain", "no chain", "cash buyers", "deceased"]


def _headers():
    return {"Authorization": f"Api-Key {HOMEDATA_API_KEY}", "Accept": "application/json"}


# ===========================================================================
# HOMEDATA  (boundary -> live listings -> optional enrichment)
# ===========================================================================
def resolve_boundary(name):
    import requests
    if _dead("homedata"):
        return None
    try:
        r = requests.get(f"{HOMEDATA_BASE}/boundaries/autocomplete/",
                         params={"q": name}, headers=_headers(), timeout=20)
        r.raise_for_status()
        results = r.json().get("results", [])
        if results:
            print(f"   boundary '{name}' -> id {results[0]['id']} ({results[0].get('name')})")
            return results[0]["id"]
        print(f"   no boundary found for '{name}'")
    except Exception as e:
        print(f"   boundary lookup failed for '{name}': {e}")
        if _throttled(e):
            _kill("homedata", "429")
    return None


def fetch_listings():
    if USE_MOCK or not HOMEDATA_API_KEY:
        return _mock_listings()
    import requests
    out = []
    for area in SEARCH["areas"]:
        if _dead("homedata"):
            break
        bid = resolve_boundary(area)
        if not bid:
            continue
        params = {"boundary_id": bid, "transaction_type": "Sale",
                  "min_price": SEARCH["min_price"], "max_price": SEARCH["max_price"],
                  "bedrooms": SEARCH["min_beds"], "page_size": 200}
        try:
            r = requests.get(f"{HOMEDATA_BASE}/live-listings/search/",
                             params=params, headers=_headers(), timeout=30)
            r.raise_for_status()
            rows = r.json().get("results") or []
        except Exception as e:
            print(f"   Homedata listings failed for {area}: {e}")
            if _throttled(e):
                _kill("homedata", "429")
                break
            continue
        print(f"   {area}: {len(rows)} listing(s)")
        out.extend(rows)
        time.sleep(0.4)
    return out


def geocode(postcodes):
    """Postcode -> (lat, lng) via postcodes.io bulk (free, no key)."""
    import requests
    out, uniq = {}, sorted({pc for pc in postcodes if pc})
    if _dead("postcodes"):
        return out
    for i in range(0, len(uniq), 100):
        if _dead("postcodes"):
            break
        chunk = uniq[i:i + 100]
        for attempt in range(3):
            try:
                r = requests.post("https://api.postcodes.io/postcodes",
                                  json={"postcodes": chunk}, timeout=30)
                r.raise_for_status()
                for item in r.json().get("result", []):
                    res = item.get("result")
                    if res and res.get("latitude"):
                        out[item["query"]] = (res["latitude"], res["longitude"])
                break
            except Exception as e:
                print(f"   geocode chunk attempt {attempt+1} failed: {e}")
                if _throttled(e):
                    _kill("postcodes", "429/403")
                    break                       # stop retrying/hammering this run
                time.sleep(1.5 * (attempt + 1))
        time.sleep(0.3)
    print(f"   geocoded {len(out)}/{len(uniq)} postcodes")
    return out


def _eff_to_band(eff):
    if not eff:
        return ""
    return ("A" if eff >= 92 else "B" if eff >= 81 else "C" if eff >= 69 else
            "D" if eff >= 55 else "E" if eff >= 39 else "F" if eff >= 21 else "G")


def enrich_property(uprn):
    if not uprn or not ENRICH:
        return {}
    if USE_MOCK or not HOMEDATA_API_KEY:
        return {"epc": _MOCK_EPC.get(str(uprn), {}), "comps": _MOCK_COMPS.get(str(uprn), [])}
    if _dead("homedata"):
        return {}
    import requests
    out = {}
    try:
        r = requests.get(f"{HOMEDATA_BASE}/epc-checker/{uprn}/", headers=_headers(), timeout=20)
        r.raise_for_status()
        rec = r.json()
        fa = rec.get("epc_floor_area")
        out["epc"] = {"floor_area_m2": int(fa) if fa else None,
                      "rating": _eff_to_band(rec.get("current_energy_efficiency")),
                      "age_band": rec.get("construction_age_band") or ""}
    except Exception as e:
        print(f"      epc {uprn} failed: {e}")
    time.sleep(0.2)
    try:
        rc = requests.get(f"{HOMEDATA_BASE}/comparables/{uprn}/", headers=_headers(), timeout=20)
        rc.raise_for_status()
        rows = rc.json().get("comparables") or []
        out["comps"] = [{"price": int(c.get("sold_let_price") or 0),
                         "date": c.get("sold_let_date") or "",
                         "m2": c.get("epc_floor_area"), "renovated": None,
                         "distance_mi": round(c["distance_meters"] / 1609, 1)
                                        if c.get("distance_meters") else None}
                        for c in rows if c.get("sold_let_price")]
    except Exception as e:
        print(f"      comparables {uprn} failed: {e}")
    return out


def listing_to_property(row):
    if row.get("is_new_build"):
        return None
    price = int(row.get("latest_price") or row.get("price") or 0)
    if not price:
        return None
    sub = (row.get("property_type") or "").lower()
    if "semi" in sub or not any(t in sub for t in TARGET_TYPES):
        return None
    ptype = ("bungalow" if "bungalow" in sub else "detached" if "detached" in sub
             else "cottage" if "cottage" in sub else (sub.split()[0] if sub else "house"))
    street = (row.get("street") or "").strip()
    postcode = (row.get("postcode") or "").strip()
    addr = ", ".join(x for x in (street, postcode) if x) or "(address withheld)"
    pid = str(row.get("id") or hashlib.sha1(addr.lower().encode()).hexdigest()[:10])
    dom = row.get("days_on_market")
    reductions = row.get("times_reduced")
    if reductions is None:
        reductions = 1 if row.get("is_reduced") or row.get("reduced_date") else 0
    return {
        "id": pid, "address": addr, "postcode": postcode, "street": street,
        "lat": None, "lng": None,
        "property_type": ptype, "beds": int(row.get("bedrooms") or 0), "price": price,
        "status": "live", "relisted_count": 0,
        "source": {"portal": "homedata", "listing_id": str(row.get("id") or ""),
                   "url": "", "agent": row.get("agent_name") or "", "uprn": str(row.get("property_uprn") or "")},
        "media": {"photo_count": 0, "has_floorplan": False, "thumb_url": ""},
        "description_raw": row.get("description") or "",
        "enrichment": {"market": {"reductions": reductions, "is_reduced": bool(row.get("is_reduced")),
                                  "status": row.get("latest_status"), "dom": dom,
                                  "added_date": row.get("added_date")}},
        "comps": [],
    }


def _haversine_mi(a, b):
    R = 3958.8
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(h))


def _aerial_thumb(lat, lng, z=17):
    # keyless satellite tile centred near the property — shows the plot from above
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    yr = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(yr)) / math.pi) / 2.0 * n)
    return ("https://server.arcgisonline.com/ArcGIS/rest/services/"
            f"World_Imagery/MapServer/tile/{z}/{y}/{x}")


def _in_polygon(lat, lng, poly):
    x, y, inside, n = lng, lat, False, len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][1], poly[i][0]
        xj, yj = poly[j][1], poly[j][0]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


# ===========================================================================
# SCORING  (dynamic denominator - only counts signals that have data)
# ===========================================================================
def _has(text, terms):
    t = (text or "").lower()
    return [w for w in terms if w in t]


def _reno_rate(p):
    r = ((p["enrichment"].get("epc") or {}).get("rating") or "").upper()
    return (RENO_RATE_PER_M2["poor"] if r in ("F", "G") else
            RENO_RATE_PER_M2["dated"] if r in ("E", "D") else RENO_RATE_PER_M2["fair"])


def _renovated_comp(p):
    fa = (p["enrichment"].get("epc") or {}).get("floor_area_m2")
    ppm2 = sorted(c["price"] / c["m2"] for c in p["comps"] if c.get("m2"))
    if fa and ppm2:
        return int(ppm2[len(ppm2) // 2] * fa * 1.15)
    return None


def _local_density(lat, lng, rad=0.0035):
    """Count parcels whose centre lies within ~380m; low = rural/open setting."""
    if lat is None or lng is None:
        return None
    n = 0
    for p in _load_parcels():
        b = p["b"]
        cy = (b[0] + b[1]) / 2
        cx = (b[2] + b[3]) / 2
        if abs(cy - lat) < rad and abs(cx - lng) < rad:
            n += 1
    return n


def build_reasons(p):
    """Two or three plain-language reasons this surfaced - the card headline."""
    m = p["enrichment"].get("market") or {}
    plot = p["enrichment"].get("plot") or {}
    eq = p["enrichment"].get("equity") or {}
    flags = p.get("flags") or []
    t = p["property_type"]; dom = m.get("dom") or 0; red = m.get("reductions") or 0
    out = []
    acres = plot.get("area_acres")
    if acres is not None:
        out.append(f"{acres:.2f}-acre plot")
    if eq.get("equity_gain") not in (None, 0) and eq.get("equity_gain", 0) > 0:
        out.append(f"~£{eq['equity_gain']:,} potential equity")
    if t in ("land", "plot"):
        out.append("Plot / land — build potential")
    elif t in ("farm", "smallholding", "equestrian"):
        out.append(f"{t.title()} — space & potential")
    elif t == "bungalow":
        out.append("Bungalow — extend / remodel")
    elif t == "cottage":
        out.append("Period cottage — doer-upper")
    if "sold_as_seen" in flags:
        out.append("Sold as seen — renovation")
    if "cash_buyers_only" in flags:
        out.append("Cash buyers only — value play")
    if red >= 2:
        out.append(f"Cut {red}× — motivated seller")
    elif "price_reduced" in flags:
        out.append("Recently reduced")
    if dom > 120:
        out.append(f"{dom} days unsold")
    if "priced_just_above" in flags:
        out.append("Priced just above a round number")
    if p.get("low_comp"):
        out.append("Low competition")
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r); uniq.append(r)
    p["reasons"] = uniq[:3] or ["In your target area"]
    return p


def detect_flags(p):
    flags = []; desc = (p["description_raw"] or "").lower()
    market = p["enrichment"].get("market") or {}
    if market.get("reductions"): flags.append("price_reduced")
    if (market.get("dom") or 0) > 120: flags.append("stale_listing")
    for thr in (500_000, 600_000, 650_000, 700_000, 750_000):
        if thr < p["price"] <= thr * 1.02:
            flags.append("priced_just_above"); break
    if "cash buyer" in desc or "cash only" in desc: flags.append("cash_buyers_only")
    if "sold as seen" in desc: flags.append("sold_as_seen")
    return flags


# ===========================================================================
# STORAGE
# ===========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (id TEXT PRIMARY KEY, first_seen TEXT,
  last_seen TEXT, price INTEGER, payload TEXT);
CREATE TABLE IF NOT EXISTS price_history (id TEXT, date TEXT, price INTEGER);
CREATE TABLE IF NOT EXISTS geocache (postcode TEXT PRIMARY KEY, lat REAL, lng REAL);
CREATE TABLE IF NOT EXISTS epccache (k TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS apicache (k TEXT PRIMARY KEY, data TEXT);
"""


def _cache_get(conn, k):
    """Read a persisted API result (constraints/planning) from SQLite. None = miss."""
    if conn is None:
        return None
    row = conn.execute("SELECT data FROM apicache WHERE k=?", (k,)).fetchone()
    if row:
        try:
            return json.loads(row["data"])
        except Exception:
            return None
    return None


def _cache_put(conn, k, val):
    """Persist an API result so we never re-hit a rate-limited source for the same point."""
    if conn is None:
        return
    try:
        conn.execute("INSERT OR REPLACE INTO apicache VALUES (?,?)", (k, json.dumps(val)))
        conn.commit()
    except Exception:
        pass


def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA); return conn


def load_geocache(conn):
    return {r["postcode"]: (r["lat"], r["lng"])
            for r in conn.execute("SELECT postcode, lat, lng FROM geocache")}


def seed_geocache_from_properties(conn):
    """Recover coords saved in past runs so a geocode outage can't blank us."""
    n = 0
    for r in conn.execute("SELECT payload FROM properties"):
        try:
            p = json.loads(r["payload"])
            pc, lat, lng = p.get("postcode"), p.get("lat"), p.get("lng")
            if pc and lat is not None and lng is not None:
                conn.execute("INSERT OR IGNORE INTO geocache VALUES (?,?,?)", (pc, lat, lng))
                n += 1
        except Exception:
            pass
    conn.commit(); return n


def save_geocache(conn, coords):
    for pc, (lat, lng) in coords.items():
        conn.execute("INSERT OR REPLACE INTO geocache VALUES (?,?,?)", (pc, lat, lng))
    conn.commit()


def recover_from_db(conn):
    """Last-known in-area properties, for when fresh data can't be fetched."""
    seen = {}
    for r in conn.execute("SELECT payload FROM properties"):
        try:
            p = json.loads(r["payload"])
            if str(p.get("id","")).startswith("hd_"):
                continue
            if p.get("lat") is not None and _in_polygon(p["lat"], p["lng"], AREA_POLYGON):
                seen[p["id"]] = p
        except Exception:
            pass
    return sorted(seen.values(), key=lambda x: -x.get("score", 0))


_PARCELS = None


def _load_parcels():
    global _PARCELS
    if _PARCELS is not None:
        return _PARCELS
    try:
        with gzip.open(PLOTS_FILE, "rt") as f:
            _PARCELS = json.load(f).get("parcels", [])
        print(f"- loaded {len(_PARCELS)} INSPIRE parcels for plot sizing")
    except Exception as e:
        print(f"- plot data unavailable ({e}); plot gate disabled")
        _PARCELS = []
    return _PARCELS


def _pip(lat, lng, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if ((xi > lng) != (xj > lng)) and (lat < (yj - yi) * (lng - xi) / (xj - xi) + yi):
            inside = not inside
        j = i
    return inside


def plot_for(lat, lng):
    """Plot area (m2) of the parcel containing this point - smallest containing parcel."""
    if lat is None or lng is None:
        return None
    best = None
    for p in _load_parcels():
        b = p["b"]
        if b[0] <= lat <= b[1] and b[2] <= lng <= b[3] and _pip(lat, lng, p["r"]):
            if best is None or p["a"] < best:
                best = p["a"]
    return best


_FOOTPRINTS = None


def _load_footprints():
    global _FOOTPRINTS
    if _FOOTPRINTS is not None:
        return _FOOTPRINTS
    try:
        with gzip.open(FOOTPRINTS_FILE, "rt") as f:
            _FOOTPRINTS = json.load(f).get("buildings", [])
        print(f"- loaded {len(_FOOTPRINTS)} building footprints")
    except Exception as e:
        print(f"- footprint data unavailable ({e})")
        _FOOTPRINTS = []
    return _FOOTPRINTS


def parcel_for(lat, lng):
    """Smallest parcel whose boundary contains the point (the property's own plot)."""
    if lat is None or lng is None:
        return None
    best = None
    for p in _load_parcels():
        b = p["b"]
        if b[0] <= lat <= b[1] and b[2] <= lng <= b[3] and _pip(lat, lng, p["r"]):
            if best is None or p["a"] < best["a"]:
                best = p
    return best


def analyze_buildings(parcel):
    """Footprints inside a parcel -> main house, outbuildings, plot coverage.
    Powers outbuilding/conversion detection and a floor-area fallback."""
    if not parcel:
        return {}
    b, ring = parcel["b"], parcel["r"]
    inside = []
    for f in _load_footprints():
        la, lo = f["c"]
        if b[0] <= la <= b[1] and b[2] <= lo <= b[3] and 0 < f["a"] < 50000 and _pip(la, lo, ring):
            inside.append(f["a"])
    if not inside:
        return {}
    inside.sort(reverse=True)
    main = inside[0]
    secondary = [a for a in inside[1:] if a >= 25]
    largest_sec = secondary[0] if secondary else 0
    cov = round(100 * sum(inside) / parcel["a"]) if parcel.get("a") else None
    out = {"main_m2": main, "buildings": len(inside),
           "largest_secondary_m2": largest_sec, "coverage_pct": cov}
    if largest_sec >= 100:
        out["outbuilding"] = "large"
    elif largest_sec >= 60:
        out["outbuilding"] = "medium"
    return out



def _excluded_type(typ):
    t = (typ or "").lower()
    return any(k in t for k in ("semi", "terrace", "flat", "maisonette", "apartment"))


def _addr_is_flat(addr):
    """Flat/apartment given away by the address itself (e.g. 'Flat 2, ...')."""
    return bool(re.search(r"\b(flat|apartment|apt|maisonette|penthouse|bedsit|"
                          r"studio flat)\b", addr or "", re.I))


_CONSTRAINT_CACHE = {}
_PLANIT_CACHE = {}
_CONSTRAINT_PENALTY = {"green-belt": 0.25, "area-of-outstanding-natural-beauty": 0.12,
                       "conservation-area": 0.10, "article-4-direction-area": 0.08,
                       "listed-building": 0.30, "flood-risk-zone": 0.10}
_CONSTRAINT_LABEL = {"green-belt": "Green Belt", "area-of-outstanding-natural-beauty": "AONB",
                     "conservation-area": "Conservation Area", "article-4-direction-area": "Article 4",
                     "listed-building": "Listed", "flood-risk-zone": "Flood zone"}


def fetch_constraints(lat, lng, conn=None):
    """Planning constraints at a point via planning.data.gov.uk (free, keyless).
    Returns {list, feasibility}. Feasibility is a 0.5-1.0 multiplier on the score.
    Results persist in SQLite (constraints are stable), so we hit the live source
    at most once per point ever - protecting access and keeping scores consistent."""
    if lat is None or lng is None:
        return {}
    key = (round(lat, 5), round(lng, 5))
    if key in _CONSTRAINT_CACHE:
        return _CONSTRAINT_CACHE[key]
    ck = f"CON|{key[0]}|{key[1]}"
    cached = _cache_get(conn, ck)
    if cached is not None:
        _CONSTRAINT_CACHE[key] = cached
        return cached
    if _dead("planning_data"):
        return {}
    import requests
    params = [("latitude", lat), ("longitude", lng), ("limit", 100)]
    for d in _CONSTRAINT_PENALTY:
        params.append(("dataset", d))
    try:
        r = requests.get("https://www.planning.data.gov.uk/entity.json", params=params,
                         headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=25)
        r.raise_for_status()
        data = r.json()
        ents = data.get("entities") or data.get("results") or []
        found = sorted({e.get("dataset") for e in ents if e.get("dataset") in _CONSTRAINT_PENALTY})
        grade = ""
        for e in ents:
            if e.get("dataset") == "listed-building":
                grade = e.get("listed_building_grade") or e.get("listed-building-grade") or ""
                break
    except Exception as ex:
        print(f"   constraints fetch failed ({lat},{lng}): {ex}")
        if _throttled(ex):
            _kill("planning_data", "429")
        return {}
    res = {"list": [_CONSTRAINT_LABEL[d] for d in found], "datasets": found, "grade": grade}
    _CONSTRAINT_CACHE[key] = res
    _cache_put(conn, ck, res)
    return res


def fetch_planning_history(lat, lng, krad=0.5, conn=None):
    """Local planning approval rate via PlanIt (free, keyless). Returns approvals vs
    refusals within krad km, last ~12 years - real precedent for 'will they permit'.
    Persisted in SQLite: PlanIt rate-limits (429), so re-querying the same point every
    nightly run is exactly what gets us throttled. Cache once, reuse thereafter."""
    if lat is None or lng is None:
        return {}
    key = (round(lat, 4), round(lng, 4))
    if key in _PLANIT_CACHE:
        return _PLANIT_CACHE[key]
    ck = f"PLANIT|{key[0]}|{key[1]}|{krad}"
    cached = _cache_get(conn, ck)
    if cached is not None:
        _PLANIT_CACHE[key] = cached
        return cached
    if _dead("planit"):
        return {}
    import requests
    from datetime import date
    start = f"{date.today().year - 12}-01-01"
    params = {"lat": lat, "lng": lng, "krad": krad, "pg_sz": 100,
              "start_date": start, "end_date": str(date.today())}
    try:
        r = requests.get("https://www.planit.org.uk/api/applics/json", params=params,
                         headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=25)
        r.raise_for_status()
        recs = r.json().get("records", [])
    except Exception as ex:
        print(f"   planning history fetch failed ({lat},{lng}): {ex}")
        if _throttled(ex):
            _kill("planit", "429")
        return {}
    approved = refused = 0
    for a in recs:
        st = (a.get("app_state") or "").lower()
        if st in ("permitted", "conditions"):
            approved += 1
        elif st in ("rejected", "refused"):
            refused += 1
    decided = approved + refused
    rate = (approved / decided) if decided else None
    res = {"n": len(recs), "approved": approved, "refused": refused,
           "decided": decided, "rate": rate}
    _PLANIT_CACHE[key] = res
    _cache_put(conn, ck, res)
    return res


def permission_estimate(datasets, planning):
    """Combine local approval rate with the constraint stack into a 0-1 development-
    permission likelihood. Heuristic, not a guarantee - precedent + rules."""
    rate = (planning or {}).get("rate")
    decided = (planning or {}).get("decided", 0)
    base = rate if (rate is not None and decided >= 5) else 0.75
    drag = 0.0
    for d in (datasets or []):
        drag += {"green-belt": 0.30, "area-of-outstanding-natural-beauty": 0.15,
                 "listed-building": 0.25, "article-4-direction-area": 0.10,
                 "conservation-area": 0.08, "flood-risk-zone": 0.05}.get(d, 0)
    est = max(0.05, min(1.0, base - drag))
    label = "development-friendly" if est >= 0.70 else ("mixed" if est >= 0.50 else "restricted")
    return {"estimate": round(est, 2), "label": label}


def _band01(x, lo, hi):
    if x is None or hi == lo:
        return 0.5
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def score_property(p):
    """Differentiated composite score across five axes. Feasibility is a
    placeholder (1.0) until the constraint layer (Green Belt / AONB) is added."""
    home = HOME_PLOT_M2 or 380
    plot = p.get("plot_m2")
    approx = "approx-location" in (p.get("flags") or [])   # plot not parcel-precise
    if plot:
        ratio = plot / home
        # Diminishing returns so no single axis runs away with the score: a plot
        # meaningfully bigger than yours is great, but a 5x plot is not 5x better than a
        # 2x plot for a forever home. Saturating curve (~0.47 at 1x, ~0.59 at 2x, tops
        # ~0.90 from plot alone) - reaching the top still needs the dev-room bonus and
        # the other axes. Replaces the old linear ramp that maxed Potential by ~3.4x.
        eff, clipped = ratio, False
        if approx and ratio > 4.0:
            # An UNVERIFIED "huge" plot is the classic centroid-in-the-enclosing-parcel
            # error (a neighbour's/estate's parcel). Cap the reward at ~4x so a number we
            # cannot trust can't dominate; a UPRN-precise plot keeps its full ratio.
            eff, clipped = 4.0, True
        potential = 0.28 + 0.62 * eff / (eff + 2.0)
        pot_note = f"{plot:,} m\u00b2 plot ({ratio:.1f}\u00d7 your home)"
        if clipped:
            pot_note += " \u00b7 approx, capped"
    else:
        ratio, potential, pot_note = 0, 0.35, "plot unverified"
    fb = p.get("footprints") or {}
    cov = fb.get("coverage_pct")
    if plot and cov is not None and cov <= 12:
        potential = min(1.0, potential + 0.08)
        pot_note += f" · {cov}% built (room to develop)"
    price = p.get("price") or p.get("est_mid")
    est_mid = p.get("est_mid")
    if price:
        # TRUE value = how far the asking price sits BELOW comparable sold evidence.
        # Only meaningful when we hold an independent asking price AND a comp-based
        # estimate that differ (probate leads price==est_mid, so they fall through to
        # the affordability prior). Trust scales with the number of comps. A wider
        # comp band means MORE uncertainty, so - unlike before - it never lifts value.
        if est_mid and est_mid > 0 and abs(est_mid - price) > max(1, 0.005 * est_mid):
            discount = max(-0.5, min(0.5, (est_mid - price) / est_mid))  # +ve = under comps
            reliable = _band01(p.get("est_n") or 0, 3, 12)               # 3 comps->0, 12+->1
            value = min(1.0, max(0.0, 0.5 + discount * (0.6 + 0.4 * reliable)))
            _sign = "+" if discount >= 0 else ""
            val_note = (f"\u00a3{round(price/1000)}k vs \u00a3{round(est_mid/1000)}k comps "
                        f"({_sign}{round(discount*100)}%)")
        else:
            # Fallback prior: no comp evidence yet (e.g. public listings) - reward
            # absolute affordability within the target range. Weaker than a real discount.
            value = 1.0 - _band01(price, PROBATE_MIN_PRICE, PROBATE_MAX_PRICE)
            val_note = f"~\u00a3{round(price/1000)}k"
    else:
        value, val_note = 0.4, "price unknown"
    d = p.get("dist_mi")
    fit = max(0.0, 1.0 - (d if d is not None else 6) / 12.0)
    fit_note = f"{d} mi from home" if d is not None else "distance n/a"
    edge, ebits = 0.0, []
    fl = p.get("flags") or []
    if "probate" in fl:
        edge += 0.10; ebits.append("probate")
    if "auction" in fl:
        edge += 0.08; ebits.append("auction")
    dom = ((p.get("enrichment", {}).get("market", {}) or {}).get("dom")
           or p.get("days_on_market") or 0)
    if dom and dom >= 120:
        edge += 0.06; ebits.append(f"{dom}d on market")
    txt = (p.get("address", "") + " " + " ".join(p.get("reasons", []))).lower()
    if "reduc" in txt:
        edge += 0.06; ebits.append("reduced")
    if any(k in txt for k in ("cash only", "cash buyers", "unmortgageable")):
        edge += 0.05; ebits.append("cash-only")
    edge = min(0.20, edge)
    lsec = fb.get("largest_secondary_m2") or 0
    if lsec >= 60:
        ebits.append(f"outbuilding ~{lsec} m\u00b2")
    edge_note = ", ".join(ebits) if ebits else "open market"
    feas = p.get("feasibility", 1.0)
    # Each axis's DISPLAYED contribution equals its ACTUAL contribution to the score,
    # so the breakdown reconciles with the headline (the old bars summed to 140 and
    # never matched). The composite is numerically unchanged - only reporting is honest:
    # 0.45*85*pot + 0.30*85*val + 0.25*85*fit == old core*85.
    pot_pts = 0.45 * 85 * potential   # up to ~38
    val_pts = 0.30 * 85 * value       # up to ~26
    fit_pts = 0.25 * 85 * fit         # up to ~21
    edge_pts = edge * 100             # up to 20 (edge already capped at 0.20)
    raw = pot_pts + val_pts + fit_pts + edge_pts
    p["score"] = int(round(min(100, raw * feas)))
    perm = p.get("permission")
    setting = p.get("setting")
    rural = setting is not None and setting <= 12
    outb = (p.get("footprints") or {}).get("outbuilding")
    # typology: development/setting/conversion plays lead when they stack up
    if outb == "large" and ratio >= 1.5 and (perm is None or perm >= 0.55):
        typ = "Conversion play"
    elif ratio >= 2.0 and perm is not None and perm >= 0.70:
        typ = "Development play"
    elif ratio >= 1.6 and rural and (perm is None or perm >= 0.55):
        typ = "Setting play"
    elif ratio >= 2.0:
        typ = "Plot play"
    elif price and value >= 0.7:
        typ = "Anomaly"
    elif edge >= 0.10:
        typ = "Motivated seller"
    elif fit >= 0.7 and ratio >= 1.0:
        typ = "Forever-fit"
    else:
        typ = "Candidate"
    p["typology"] = typ
    p["tier"] = "High" if p["score"] >= 62 else ("Medium" if p["score"] >= 48 else "Low")
    sig = {
        "Potential": {"score": round(pot_pts), "max": 38, "note": pot_note},
        "Value": {"score": round(val_pts), "max": 26, "note": val_note},
        "Fit": {"score": round(fit_pts), "max": 21, "note": fit_note},
        "Edge": {"score": round(edge_pts), "max": 20, "note": edge_note},
    }
    if perm is not None:
        # feasibility is a MULTIPLIER on the whole score, not an additive axis - show it
        # as such (a restricted site scales the total down, it does not add points).
        ph = p.get("planning") or {}
        note = f"×{feas:.2f} · {p.get('permission_label','')}".strip(" ·")
        if ph.get("decided"):
            note += f" · {ph['approved']} approved / {ph['refused']} refused nearby"
        sig["Feasibility"] = {"score": round(feas * 100), "max": 100, "note": note}
    p["signals"] = sig
    return p


def apply_gates(props, conn=None):
    """Stamp plot size; apply detached + plot-size hard gates (keep unverified plots)."""
    out = []
    dropped_type = dropped_plot = dropped_area = 0
    for p in props:
        if p.get("lat") is not None and not _in_polygon(p["lat"], p["lng"], AREA_POLYGON):
            dropped_area += 1
            continue
        _pcd_parts = (p.get("postcode") or "").split()
        _pcd = _pcd_parts[0].upper() if _pcd_parts else ""
        if _pcd in EXCLUDE_DISTRICTS:
            dropped_area += 1
            continue
        _txt = " ".join(str(p.get(k) or "") for k in ("address", "owner_note", "notice_sample")) \
            + " " + str((p.get("estate_contact") or {}).get("name") or "")
        if _is_carehome(_txt) or _is_excluded_locality(_txt):
            dropped_area += 1
            continue
        if DETACHED_ONLY and (_excluded_type(p.get("property_type")) or _addr_is_flat(p.get("address"))):
            dropped_type += 1
            continue
        lat, lng = p.get("lat"), p.get("lng")
        parcel = parcel_for(lat, lng)
        area = parcel["a"] if parcel else None
        # Sanity: a postcode-centroid coordinate often lands inside a big ENCLOSING parcel
        # (a whole estate/field) rather than the property's own plot. Publishing that as
        # "the plot" produced nonsense like a 4,323 m2 plot with a 3,479 m2 house on a semi.
        # Reject implausible matches outright rather than surface bad data.
        if area is not None and area > MAX_PLAUSIBLE_PLOT_M2:
            parcel, area = None, None
            fl = p.setdefault("flags", [])
            if "plot-unverified" not in fl:
                fl.append("plot-unverified")
        p["plot_m2"] = area
        if area is not None and area < MIN_PLOT_M2:
            dropped_plot += 1
            continue
        if area is None:
            fl = p.setdefault("flags", [])
            if "plot-unverified" not in fl:
                fl.append("plot-unverified")
        fb = analyze_buildings(parcel)
        # A "main building" bigger than a large house, or near-total plot coverage, means the
        # footprint polygon is a merged terrace/estate block - not this dwelling. Discard.
        if fb and (fb.get("main_m2", 0) > MAX_PLAUSIBLE_MAIN_M2
                   or (fb.get("coverage_pct") or 0) > MAX_PLAUSIBLE_COVERAGE):
            fb = {}
        if fb:
            p["footprints"] = fb
            if fb.get("main_m2") and not p.get("floor_area_m2"):
                # footprint -> internal floor area: x storeys x ~0.9 (walls/circulation).
                # Bungalows/single-storey are one floor, not two - halving the error there.
                _t = (p.get("property_type") or "").lower()
                _storeys = 1 if any(k in _t for k in ("bungalow", "single storey", "single-storey")) else 2
                p["floor_area_est_m2"] = round(fb["main_m2"] * _storeys * 0.9)
        con = fetch_constraints(lat, lng, conn)
        if con:
            p["constraints"] = con["list"]
            if con.get("grade"):
                p["listed_grade"] = con["grade"]
            ph = fetch_planning_history(lat, lng, conn=conn)   # keyword: conn is NOT krad
            if ph:
                p["planning"] = ph
            perm = permission_estimate(con.get("datasets"), ph)
            p["permission"] = perm["estimate"]
            p["permission_label"] = perm["label"]
            p["feasibility"] = max(0.4, perm["estimate"])
        # A plot/footprint matched from a POSTCODE-CENTROID coordinate is only indicative
        # (it can be the enclosing parcel). Flag it - UNLESS we snapped the property onto
        # its precise UPRN coordinate, in which case the match is this dwelling's own.
        if (p.get("plot_m2") or p.get("footprints")) and not p.get("precise_location"):
            fl = p.setdefault("flags", [])
            if "approx-location" not in fl:
                fl.append("approx-location")   # postcode centroid - plot/footprint indicative
        p["setting"] = _local_density(lat, lng)
        score_property(p)
        out.append(p)
    if dropped_type or dropped_plot or dropped_area:
        print(f"- gates: dropped {dropped_area} out-of-area, {dropped_type} non-detached, {dropped_plot} under {MIN_PLOT_M2} m2 plot")
    return out


# --- Personal data must not reach a committed artifact. The site runs on free
# GitHub Pages, so BOTH docs/properties.json (served publicly) and the
# workflow-committed data/scout.db live in a public repo. The Gazette deceased-
# estates notices are public statutory records, but aggregating names + executor
# contacts + notice text into a crawlable file is a standing re-publication we
# don't want. So strip person-level data at every serialization boundary and keep
# only the notice URL - the user opens the (already-public) notice on demand. ---
_PII_KEYS = ("estate_contact", "notice_sample", "uprn")


def _public_safe(p):
    """Return a copy of a property with personal data removed, for anything that
    gets committed (public Pages JSON + the repo's scout.db). Runtime gates that
    read owner_note / estate_contact still see the full in-memory dict; only the
    serialized copy is sanitised."""
    q = dict(p)
    for k in _PII_KEYS:
        q.pop(k, None)
    if q.get("owner_note"):
        q["owner_note"] = "Probate lead — see notice"   # drop the deceased's name
    addr = q.get("address") or ""
    if addr.startswith("Estate of "):                        # name-only address fallback
        q["address"] = (f"Probate lead — {q.get('postcode', '')}").strip(" —")
    src = q.get("source")
    if isinstance(src, dict) and src.get("uprn"):
        src = dict(src); src["uprn"] = ""; q["source"] = src
    return q


def _publish(props, conn=None):
    props = apply_gates(props, conn)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(),
         "count": len(props), "home_floor_m2": HOME_FLOOR_AREA_M2,
         "home_footprint_m2": HOME_FOOTPRINT_M2,
         "home_plot_m2": HOME_PLOT_M2, "min_plot_m2": MIN_PLOT_M2,
         "home_plot_outline": HOME_PLOT_OUTLINE,
         "properties": [_public_safe(p) for p in props]}, indent=2))


def upsert(conn, p):
    today = date.today().isoformat()
    row = conn.execute("SELECT first_seen, price FROM properties WHERE id=?", (p["id"],)).fetchone()
    if row:
        p["first_seen"] = row["first_seen"]
        if row["price"] != p["price"]:
            conn.execute("INSERT INTO price_history VALUES (?,?,?)", (p["id"], today, p["price"]))
    else:
        p["first_seen"] = today
        conn.execute("INSERT INTO price_history VALUES (?,?,?)", (p["id"], today, p["price"]))
    p["last_seen"] = today
    ph = conn.execute("SELECT date, price FROM price_history WHERE id=? ORDER BY date", (p["id"],)).fetchall()
    p["price_history"] = [dict(r) for r in ph]
    conn.execute("INSERT INTO properties VALUES (?,?,?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, "
                 "price=excluded.price, payload=excluded.payload",
                 (p["id"], p["first_seen"], p["last_seen"], p["price"],
                  json.dumps(_public_safe(p))))
    conn.commit()


def existing(conn, pid):
    row = conn.execute("SELECT payload, price FROM properties WHERE id=?", (pid,)).fetchone()
    return (json.loads(row["payload"]), row["price"]) if row else (None, None)


# ===========================================================================
# MAIN
# ===========================================================================
_WORD_NUM = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5,
             "SIX": 6, "SEVEN": 7, "EIGHT": 8}


def _strip_tags(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)).strip()


def _first_price(t):
    m = re.search(r"\u00a3\s*([\d,]+)", t)
    return int(m.group(1).replace(",", "")) if m else None


def _infer_type_beds(t):
    u = t.upper()
    beds = 0
    m = re.search(r"\b(ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT)[- ]BEDROOM", u)
    if m:
        beds = _WORD_NUM[m.group(1)]
    dm = re.search(r"\b([1-9])\s*BED\b", u)
    if dm:
        beds = int(dm.group(1))
    for kw, typ in [("BUNGALOW", "bungalow"), ("COTTAGE", "cottage"),
                    ("SMALLHOLDING", "smallholding"), ("STABLES", "equestrian"),
                    ("FARM", "farm"), ("WOODLAND", "land"), ("LAND", "land"),
                    ("PLOT", "plot"), ("BARN", "barn"), ("DETACHED", "detached")]:
        if kw in u:
            return typ, beds
    if "HOUSE" in u:
        return "house", beds
    if "FLAT" in u or "MAISONETTE" in u or "APARTMENT" in u:
        return "flat", beds
    return "auction lot", beds


def _clean_title(t, lot):
    t = re.sub(r"^\s*LOT\s+\d+\s*", "", t, flags=re.I).strip()
    t = re.split(r"\s+(?:AVAILABLE AT|SOLD|POSTPONED|WITHDRAWN)", t, flags=re.I)[0].strip()
    return f"Lot {lot}: {t[:80]}"


def _auction_score(t):
    u = t.upper()
    s = 50
    if any(k in u for k in ("LAND", "PLOT", "PLANNING", "ACRE", "WOODLAND", "BARN")):
        s += 18
    if any(k in u for k in ("IMPROVEMENT", "REFURBISH", "UPDATING", "REPAIR",
                            "RENOVAT", "COMPLETION", "POTENTIAL", "MODERNIS")):
        s += 14
    if any(k in u for k in ("BUNGALOW", "COTTAGE", "DETACHED")):
        s += 6
    return min(s, 92)


def _auction_reasons(t, price):
    u = t.upper()
    r = [f"Auction \u2014 guide \u00a3{price:,}" if price else "Auction lot"]
    if "ACRE" in u or "WOODLAND" in u:
        r.append("Land / acreage")
    if "PLANNING" in u:
        r.append("Planning angle")
    if any(k in u for k in ("IMPROVEMENT", "REFURBISH", "UPDATING", "REPAIR",
                            "RENOVAT", "COMPLETION", "MODERNIS")):
        r.append("Needs work \u2014 value play")
    return r[:3]


def fetch_clive_emson():
    """Clive Emson current auction: available lots within range of home.
    Independent of Homedata - works even when that quota is spent."""
    if not AUCTION_ENABLED or _dead("clive_emson"):
        return []
    import requests
    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml",
               "Accept-Language": "en-GB,en;q=0.9"}
    try:
        r = requests.get(CLIVE_EMSON_URL, headers=headers, timeout=30)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"   auction fetch failed: {e}")
        if _throttled(e):
            _kill("clive_emson", "429/403")
        return []

    coords = [(m.start(), float(m.group(1)), float(m.group(2)))
              for m in re.finditer(r"maps\?q=(-?\d+\.\d+),\s*(-?\.?\d+\.?\d*)", html)]

    lots = {}
    for m in re.finditer(r"/properties/(\d+)/(\d+)/['\"][^>]*>(.*?)</a>", html, re.S):
        auc, lot, inner = m.group(1), m.group(2), m.group(3)
        if lot in lots:
            continue
        text = _strip_tags(inner)
        if not text.upper().startswith("LOT"):
            continue
        prev = [c for c in coords if c[0] < m.start()]
        latlng = (prev[-1][1], prev[-1][2]) if prev else None
        lots[lot] = (auc, text, latlng)

    today = datetime.now(timezone.utc).date().isoformat()
    out = []
    for lot, (auc, text, latlng) in lots.items():
        if latlng is None or "AVAILABLE" not in text.upper():
            continue
        lat, lng = latlng
        if not (49.8 < lat < 53.0 and -6.5 < lng < 2.0):
            continue
        dist = _haversine_mi(HOME, (lat, lng))
        if dist > AUCTION_RADIUS_MI:
            continue
        price = _first_price(text)
        ptype, beds = _infer_type_beds(text)
        out.append({
            "id": f"ce_{auc}_{lot}", "address": _clean_title(text, lot),
            "postcode": "", "price": price or 0, "beds": beds,
            "property_type": ptype, "lat": lat, "lng": lng,
            "dist_mi": round(dist, 1), "score": _auction_score(text),
            "reasons": _auction_reasons(text, price), "flags": ["auction"],
            "is_auction": True, "low_comp": False, "comps": [],
            "source": {"name": "Clive Emson", "url":
                       f"https://www.cliveemson.co.uk/properties/{auc}/{lot}/", "uprn": ""},
            "source_label": "AUCTION",
            "enrichment": {"market": {}, "plot": {}, "equity": {}},
            "media": {"photo_count": 0, "has_floorplan": False,
                      "thumb_url": _aerial_thumb(lat, lng)},
            "first_seen": today, "last_seen": today, "days_on_market": 0,
        })
    out.sort(key=lambda x: -x["score"])
    return out


def fetch_auction_house(conn):
    """Auction House Sussex & Hampshire: available house/land/bungalow lots,
    geocoded by postcode (cached). Screens out flats/garages/commercial."""
    if not AUCTION_ENABLED or _dead("auction_house"):
        return []
    import requests
    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml",
               "Accept-Language": "en-GB,en;q=0.9"}
    try:
        r = requests.get(AUCTION_HOUSE_URL, headers=headers, timeout=30)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"   auction-house fetch failed: {e}")
        if _throttled(e):
            _kill("auction_house", "429/403")
        return []

    pc_re = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b")
    raw = []
    for m in re.finditer(r"href=\"(https://[^\"]*?(?:auction/lot/\d+|lot/redirect/\d+))\"[^>]*>(.*?)</a>",
                         html, re.S):
        url, inner = m.group(1), _strip_tags(m.group(2))
        if "SOLD" in inner.upper():            # only currently available
            continue
        u = inner.upper()
        if any(t.upper() in u for t in AUCTION_SKIP_TYPES):  # screen out flats etc
            continue
        pm = pc_re.search(inner)
        if not pm:
            continue
        lid_m = re.search(r"(?:auction/lot/|lot/redirect/)(\d+)", url)
        lid = lid_m.group(1) if lid_m else pm.group(1).replace(" ", "")
        raw.append({"url": url, "text": inner, "postcode": pm.group(1).upper().strip(), "lid": lid})

    if not raw:
        return []

    cache = load_geocache(conn)
    need = sorted({x["postcode"] for x in raw if x["postcode"] not in cache})
    fresh = geocode(need) if need else {}
    save_geocache(conn, fresh)
    coords = {**cache, **fresh}

    today = datetime.now(timezone.utc).date().isoformat()
    out, seen = [], set()
    for x in raw:
        if x["lid"] in seen:
            continue
        ll = coords.get(x["postcode"])
        if not ll:
            continue
        lat, lng = ll
        dist = _haversine_mi(HOME, (lat, lng))
        if dist > AUCTION_RADIUS_MI:
            continue
        seen.add(x["lid"])
        price = _first_price(x["text"])
        ptype, beds = _infer_type_beds(x["text"])
        addr = re.split(r"\(plus fees\)", x["text"])[-1].strip(" -")
        addr = re.sub(r"^Lot\s*-?\s*", "", addr).strip()
        out.append({
            "id": f"ah_{x['lid']}", "address": addr or f"Auction lot {x['lid']}",
            "postcode": x["postcode"], "price": price or 0, "beds": beds,
            "property_type": ptype, "lat": lat, "lng": lng,
            "dist_mi": round(dist, 1), "score": _auction_score(x["text"]),
            "reasons": _auction_reasons(x["text"], price), "flags": ["auction"],
            "is_auction": True, "low_comp": False, "comps": [],
            "source": {"name": "Auction House", "url": x["url"], "uprn": ""},
            "source_label": "AUCTION",
            "enrichment": {"market": {}, "plot": {}, "equity": {}},
            "media": {"photo_count": 0, "has_floorplan": False,
                      "thumb_url": _aerial_thumb(lat, lng)},
            "first_seen": today, "last_seen": today, "days_on_market": 0,
        })
    return out


def fetch_auction_lots(conn):
    """All auction sources, combined."""
    lots = []
    ce = fetch_clive_emson()
    print(f"   Clive Emson: {len(ce)} lot(s) in range")
    lots += ce
    ah = fetch_auction_house(conn)
    print(f"   Auction House: {len(ah)} lot(s) in range")
    lots += ah
    return lots


def _probate_name(title, content):
    name = (title or "").strip()
    if not name:
        name = content[:60].strip()
    name = re.sub(r"\s*\(?deceased\)?\.?\s*$", "", name, flags=re.I).strip(" .,")
    return name or "Deceased estate"


def _probate_contact(content, deceased_pc):
    """Parse the estate's claims contact from a Gazette deceased-estates notice.
    Colon-tolerant (the search feed and the notice page label fields differently);
    falls back to prose firm-name detection."""
    content = re.sub(r"\s+", " ", content or "").strip()
    if not content:
        return {}
    STOP = (r"(?=\s*(?:Address|Town|County|Postcode|Legal|Notice|Reference|Executor|"
            r"Administrator|Personal Representative|Deceased|Date of|Claims|Previous|$))")
    ref = ""
    rm = re.search(r"Reference(?:\s+Number)?:?\s+([A-Za-z0-9/.\-]+)", content, re.I)
    if rm:
        ref = rm.group(1).strip(" .")
    name = ""
    # structured company executor/administrator (colon optional; several label forms)
    for lbl in (r"Executor/Administrator Company Name",
                r"Personal Representative Company Name",
                r"Name of Personal Representative",
                r"Company Name"):
        cm = re.search(lbl + r":?\s+(.+?)" + STOP, content, re.I)
        if cm and cm.group(1).strip(" -\u2013,"):
            name = cm.group(1).strip(" -\u2013,")
            break
    if not name:  # structured person executor (title/first/surname labels)
        parts = []
        for lbl in (r"Executor/Administrator Title", r"Executor/Administrator First name",
                    r"Executor/Administrator Surname", r"Personal Representative Title",
                    r"Personal Representative First name", r"Personal Representative Surname"):
            m = re.search(lbl + r":?\s+(.+?)" + STOP, content, re.I)
            if m and m.group(1).strip():
                parts.append(m.group(1).strip())
        name = " ".join(parts).strip()
    if not name:  # prose fallback: legal-suffix firm anywhere
        _SUF = (r"(?:Solicitors|Solicitor|LLP|& Co\.?|& Partners|Law Firm|Legal|"
                r"Trust Corporation|Limited|Ltd|Partners)")
        fm = re.search(r"((?:[A-Z][\w'.]*\s+(?:&\s+)?){1,4}" + _SUF + r")\b", content)
        if fm:
            name = fm.group(1).strip()
    if not name:
        return {}
    # executor postcode: first postcode appearing AFTER the firm/executor name
    contact_pc = ""
    pos = content.find(name)
    tail = content[pos + len(name):] if pos >= 0 else content
    pcm = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", tail)
    if pcm and pcm.group(1).upper() != (deceased_pc or "").upper():
        contact_pc = pcm.group(1).upper()
    if not contact_pc:  # else last postcode that isn't the deceased's
        for pc in reversed(re.findall(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", content)):
            if pc.upper() != (deceased_pc or "").upper():
                contact_pc = pc.upper()
                break
    phone = ""
    pm2 = re.search(r"Telephone:?\s+([0-9][0-9\s()+]{6,})", content)
    if pm2:
        phone = re.sub(r"\s{2,}", " ", pm2.group(1)).strip(" .,")
    email = ""
    em = re.search(r"Email(?:\s+address)?:?\s+([^\s]+@[^\s]+)", content, re.I)
    if em:
        email = em.group(1).strip(" .,")
    kind = "solicitor" if re.search(r"\b(Solicitors?|LLP|& Co|Legal|Law|Trust Corporation)\b",
                                    name, re.I) else "executor"
    return {"name": name[:60], "ref": ref, "postcode": contact_pc,
            "phone": phone, "email": email, "kind": kind}


_NOTICE_CACHE = {}


def _fetch_notice_text(url):
    """The search feed omits the executor block; the notice's own page carries it
    in labelled fields (Executor/Administrator Company Name, Postcode, Reference).
    Fetch that page and flatten to text. Cached; fails soft; logs HTTP status."""
    import requests
    if not url:
        return ""
    if url in _NOTICE_CACHE:
        return _NOTICE_CACHE[url]
    if _dead("gazette"):
        return ""
    txt, made_request = "", False
    hdrs = {"User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    try:
        r = requests.get(url, headers=hdrs, timeout=20)
        made_request = True
        if r.status_code == 429:
            _kill("gazette", "429")
            _NOTICE_CACHE[url] = ""
            return ""
        if r.status_code != 200:
            print(f"   notice page HTTP {r.status_code}: {url}")
        if r.ok:
            txt = _strip_tags(r.text)
    except Exception as e:
        print(f"   notice fetch failed {url}: {e}")
        if _throttled(e):
            _kill("gazette", "429")
    if made_request:
        time.sleep(GAZETTE_CRAWL_DELAY)  # respect The Gazette's 1-request/10s crawl-delay
    _NOTICE_CACHE[url] = txt
    return txt


_PTYPE = {"detached": "detached", "semi-detached": "semi", "terraced": "terraced",
          "flat-maisonette": "flat", "other": "other"}


def _lrval(x):
    """Pull a scalar from HM Land Registry linked-data JSON (handles nesting)."""
    if x is None or isinstance(x, (str, int, float)):
        return x
    if isinstance(x, list):
        return _lrval(x[0]) if x else None
    if isinstance(x, dict):
        for k in ("_value", "prefLabel", "label", "_label", "@id"):
            if k in x:
                v = _lrval(x[k])
                if k == "@id" and isinstance(v, str):
                    return v.rstrip("/").rsplit("/", 1)[-1]
                return v
    return None


def _pp_parse(data):
    items = (data.get("result") or {}).get("items")
    if items is None:
        items = data.get("items") or []
    sales = []
    for it in items:
        price = _lrval(it.get("pricePaid"))
        if not price:
            continue
        ptype = _lrval(it.get("propertyType"))
        t = None
        if ptype:
            t = _PTYPE.get(str(ptype).lower().rsplit("/", 1)[-1], str(ptype).lower())
        addr = it.get("propertyAddress") or {}
        if isinstance(addr, list):
            addr = addr[0] if addr else {}
        paon = _lrval(addr.get("paon")) if isinstance(addr, dict) else None
        sales.append({"price": int(price), "date": str(_lrval(it.get("transactionDate")) or "")[:10],
                      "type": t, "paon": str(paon or "").upper()})
    return sales


def _pp_get(params):
    """One HM Land Registry Price Paid query (free, keyless), newest first."""
    import requests
    url = "https://landregistry.data.gov.uk/data/ppi/transaction-record.json"
    if _dead("landregistry"):
        return []
    try:
        r = requests.get(url, params={**params, "_pageSize": 150, "_sort": "-transactionDate"},
                         headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        return _pp_parse(r.json())
    except Exception as e:
        print(f"   price-paid fetch failed ({params}): {e}")
        if _throttled(e):
            _kill("landregistry", "403 blocked" if "403" in str(e) else "429")
        return []


def fetch_price_paid(postcode):
    """Sales in an exact postcode (micro-local, reliable)."""
    return _pp_get({"propertyAddress.postcode": postcode})


def fetch_price_paid_town(town):
    """Town-wide sales - coarse fallback when a postcode has no LR history."""
    return _pp_get({"propertyAddress.town": town.upper()})


def price_context(sales, paon_hint):
    """Subject house type + comparable value range from a postcode's sales."""
    if not sales:
        return {}
    ctx = {}
    ph = (paon_hint or "").strip().upper()
    subj = None
    if ph:
        for s in sales:
            if s["paon"] and (s["paon"] == ph or s["paon"].split(",")[0].strip() == ph):
                if subj is None or s["date"] > subj["date"]:
                    subj = s
    if subj:
        ctx["subject_type"] = subj["type"]
        ctx["last_price"] = subj["price"]
        ctx["last_year"] = subj["date"][:4]
    yr_cut = datetime.now(timezone.utc).year - 7
    recent = [s for s in sales if s["date"][:4].isdigit() and int(s["date"][:4]) >= yr_cut]
    if subj and subj["type"]:
        same = [s for s in recent if s["type"] == subj["type"]]
        if len(same) >= 3:
            recent = same
    prices = sorted(s["price"] for s in recent)
    if not prices:
        prices = sorted(s["price"] for s in sales)[-8:]
    if prices:
        n = len(prices)
        ctx.update({"est_low": prices[max(0, n // 4 - 1)],
                    "est_high": prices[min(n - 1, (3 * n) // 4)],
                    "est_mid": prices[n // 2], "n_comps": n})
        if not ctx.get("subject_type"):
            types = [s["type"] for s in recent if s["type"]]
            if types:
                ctx["subject_type"] = max(set(types), key=types.count)
    return ctx



# ===========================================================================
# LOCAL PRICE PAID VALUATION
# Land Registry blocks live API calls from cloud IPs (403), so sold prices come
# from a local file built once from their free bulk download - same pattern as
# the INSPIRE parcels and OS footprints, which have never failed.
# ===========================================================================
_PPD = None
_PPD_INDEX = None


def _load_ppd():
    global _PPD
    if _PPD is not None:
        return _PPD
    try:
        with gzip.open(PPD_FILE, "rt") as f:
            _PPD = json.load(f).get("sales", [])
        print(f"- loaded {len(_PPD):,} local Price Paid sales")
    except Exception as e:
        print(f"- local price-paid data unavailable ({e}); falling back to API")
        _PPD = []
    return _PPD


def _ppd_year_index():
    """Build a local price index from the data itself: median sale price per year.
    Lets us restate an old sale in today's money without any external HPI feed."""
    global _PPD_INDEX
    if _PPD_INDEX is not None:
        return _PPD_INDEX
    by_year = {}
    for r in _load_ppd():
        y = r["d"][:4]
        if y.isdigit():
            by_year.setdefault(int(y), []).append(r["p"])
    idx = {}
    for y, prices in by_year.items():
        if len(prices) >= 8:
            prices.sort()
            idx[y] = prices[len(prices) // 2]
    _PPD_INDEX = idx
    return idx


def _adjust_to_today(price, year):
    """Restate an old sale price in today's money using the local index."""
    idx = _ppd_year_index()
    if not idx or year not in idx:
        return price
    latest = max(idx)
    if idx[year] <= 0:
        return price
    return int(price * (idx[latest] / idx[year]))


def _sector(pc):
    pc = (pc or "").upper().strip()
    return pc.split(" ")[0] + " " + pc.split(" ")[1][:1] if " " in pc else pc




_EPC_LOCAL = None


def _load_epc_local():
    """Optional bulk EPC file keyed 'POSTCODE|PAON' -> {fa, form, rooms}.
    When present, comps become size-and-form matched (the surveyor method);
    when absent, everything degrades gracefully to the current behaviour."""
    global _EPC_LOCAL
    if _EPC_LOCAL is not None:
        return _EPC_LOCAL
    try:
        with gzip.open(EPC_LOCAL_FILE, "rt") as f:
            _EPC_LOCAL = json.load(f).get("certs", {})
        print(f"- loaded {len(_EPC_LOCAL):,} local EPC certificates for comps")
    except Exception:
        _EPC_LOCAL = {}
    return _EPC_LOCAL


def _comp_floor_area(r, conn=None):
    """Floor area for a comparable sale, from the local bulk EPC file ONLY
    (epc_region.json.gz, built by make_epc.py). NO live EPC calls for comparables:
    the register is reserved for subject lookups, so a valuation over dozens of comps
    can never burn the EPC quota. Absent bulk file -> None -> comps stay type-and-
    geography matched (not size-matched), degrading gracefully. `conn` is accepted for
    call-site compatibility and unused."""
    e = _load_epc_local().get((r["pc"] + "|" + r["n"]).upper())
    return e.get("fa") if e else None

def find_comps(postcode, paon, want_type=None, addr_line="", k=6, years=12,
               subject_fa=None, conn=None):
    """Replicate the human comparables process (the Zoopla routine):
    1. same STREET, same type, recent          - the comps a person trusts most
    2. widen to postcode, then sector, if thin
    3. restate each sale in today's money
    4. estimate = median of the chosen comps; band = their actual spread
    Returns (ctx, comp_list) - the named comps go to the app so you can judge
    them yourself, exactly as you would on a sold-prices page."""
    sales = _load_ppd()
    if not sales or not postcode:
        return {}, []
    pc = (postcode or "").upper().strip()
    sec = _sector(pc)
    cutoff = str(datetime.now(timezone.utc).year - years)
    lead_norm = _norm_addr(addr_line)
    ph = (paon or "").strip().upper()

    def street_of(r):
        return (r.get("s") or "").strip().upper()

    # which street is the subject on? longest PPD street name found in its address
    subj_street = ""
    if lead_norm:
        cands = {street_of(r) for r in sales if r["pc"] == pc and street_of(r)}
        for st in sorted(cands, key=len, reverse=True):
            if st and st in lead_norm:
                subj_street = st
                break

    pool = []
    for r in sales:
        if r["d"][:4] < cutoff:
            continue
        if want_type and r["t"] != want_type:
            continue
        if subject_fa:
            cfa = _comp_floor_area(r, conn)
            if cfa and not (0.75 * subject_fa <= cfa <= 1.30 * subject_fa):
                continue                    # a real comp is a SIMILAR-SIZED house
        if ph and r["pc"] == pc and r["n"] == ph:
            continue                                  # the subject itself isn't a comp
        if subj_street and street_of(r) == subj_street and r["pc"].split(" ")[0] == pc.split(" ")[0]:
            scope = 3                                  # same street - what a human trusts
        elif r["pc"] == pc:
            scope = 2
        elif _sector(r["pc"]) == sec:
            scope = 1
        else:
            continue
        pool.append((scope, r))
    if not pool:
        return {}, []

    best_scope = max(p[0] for p in pool)
    # prefer the tightest scope that still gives a usable handful, like a person would
    for scope_min in (3, 2, 1):
        chosen = [r for sc, r in pool if sc >= scope_min]
        if len(chosen) >= 3 or scope_min == 1:
            break
    chosen.sort(key=lambda r: r["d"], reverse=True)
    chosen = chosen[:max(k, 3)]

    adj = sorted(_adjust_to_today(r["p"], int(r["d"][:4])) for r in chosen)
    n = len(adj)
    if n < 3:
        return {}, []
    # surveyor method: when the comps have known floor areas, price per m2
    if subject_fa:
        rates = []
        for r in chosen:
            cfa = _comp_floor_area(r, conn)
            if cfa:
                rates.append(_adjust_to_today(r["p"], int(r["d"][:4])) / cfa)
        if len(rates) >= 3:
            rates.sort()
            m = len(rates)
            return ({"est_mid": int(rates[m // 2] * subject_fa),
                     "est_low": int(rates[max(0, m // 4)] * subject_fa),
                     "est_high": int(rates[min(m - 1, (3 * m) // 4)] * subject_fa),
                     "n_comps": m, "basis": "\u00a3/m\u00b2, size-matched",
                     "basis_type": want_type or "mixed"},
                    [{"addr": (r["n"] + " " + (r.get("s") or "").title()).strip(),
                      "price": r["p"], "year": r["d"][:4],
                      "adj": _adjust_to_today(r["p"], int(r["d"][:4])),
                      "pc": r["pc"]} for r in chosen])
    scope_label = {3: "same street", 2: "this postcode", 1: "nearby (" + sec + ")"}[
        3 if (subj_street and any(street_of(r) == subj_street for r in chosen)) else
        (2 if all(r["pc"] == pc for r in chosen) else 1)]
    ctx = {"est_mid": adj[n // 2],
           "est_low": adj[max(0, n // 4)],
           "est_high": adj[min(n - 1, (3 * n) // 4)],
           "n_comps": n,
           "basis": scope_label,
           "basis_type": want_type or "mixed"}
    comp_list = [{"addr": (r["n"] + " " + street_of(r).title()).strip(),
                  "price": r["p"], "year": r["d"][:4],
                  "adj": _adjust_to_today(r["p"], int(r["d"][:4])),
                  "pc": r["pc"]} for r in chosen]
    return ctx, comp_list

def price_context_local(postcode, paon, want_type=None, years=10):
    """Estimate value from local sold prices: same property type, closest
    geography, recent, restated in today's money. Returns the same shape as the
    old API-based price_context so the rest of the pipeline is unchanged."""
    sales = _load_ppd()
    if not sales:
        return {}
    pc = (postcode or "").upper().strip()
    if not pc:
        return {}
    cutoff = str(datetime.now(timezone.utc).year - years)
    sec, dist = _sector(pc), pc.split(" ")[0]

    ctx = {}
    # the subject property's own last sale - the strongest evidence there is.
    # Restated to today's money it gives a PROPERTY-SPECIFIC estimate, far
    # tighter than any area band (area bands are wide because detached stock
    # genuinely varies from cottages to manors - that is variety, not error).
    ph = (paon or "").strip().upper()
    if ph:
        own = [r for r in sales if r["pc"] == pc and r["n"] == ph]
        if own:
            last = max(own, key=lambda r: r["d"])
            ctx["subject_type"] = last["t"]
            ctx["last_price"] = last["p"]
            ctx["last_year"] = last["d"][:4]
            want_type = want_type or last["t"]
            est = _adjust_to_today(last["p"], int(last["d"][:4]))
            age = datetime.now(timezone.utc).year - int(last["d"][:4])
            band = min(0.18, 0.08 + 0.004 * age)   # index handles the market; this is condition/extension risk
            ctx.update({"est_mid": est,
                        "est_low": int(est * (1 - band)),
                        "est_high": int(est * (1 + band)),
                        "n_comps": len(own),
                        "basis": "own " + last["d"][:4] + " sale restated",
                        "basis_type": last["t"]})
            return ctx

    # widen the net only as far as needed to get a usable sample
    for scope, pred in (("postcode", lambda r: r["pc"] == pc),
                        ("sector", lambda r: _sector(r["pc"]) == sec),
                        ("district", lambda r: r["pc"].split(" ")[0] == dist)):
        pool = [r for r in sales if pred(r) and r["d"][:4] >= cutoff]
        if want_type:
            typed = [r for r in pool if r["t"] == want_type]
            if len(typed) >= 5:
                pool = typed
        if len(pool) >= 5:
            adj = sorted(_adjust_to_today(r["p"], int(r["d"][:4])) for r in pool)
            n = len(adj)
            ctx.update({"est_low": adj[max(0, n // 4 - 1)],
                        "est_high": adj[min(n - 1, (3 * n) // 4)],
                        "est_mid": adj[n // 2],
                        "n_comps": n, "basis": scope,
                        "basis_type": want_type or "all types"})
            if not ctx.get("subject_type") and want_type:
                ctx["subject_type"] = want_type
            return ctx
    return ctx

def _streetview_url(lat, lng):
    return f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lng}"


def homedata_epc(conn, postcode, paon):
    """Floor area (m2) for a property via Homedata: postcode -> UPRN -> EPC.
    Reuses your existing key; cached so each property is fetched at most once."""
    if not HOMEDATA_API_KEY or USE_MOCK:
        return {}
    if _dead("homedata"):
        return {}
    key = f"{postcode}|{paon}".upper()
    row = conn.execute("SELECT data FROM epccache WHERE k=?", (key,)).fetchone()
    if row:
        try:
            return json.loads(row["data"])
        except Exception:
            return {}
    import requests
    uprn = None
    try:
        r = requests.get(f"{HOMEDATA_BASE}/address/postcode/{postcode.replace(' ', '%20')}/",
                         headers=_headers(), timeout=20)
        r.raise_for_status()
        js = r.json()
        addrs = (js.get("addresses") or js.get("results")
                 or (js if isinstance(js, list) else []))
        ph = (paon or "").strip().upper()
        for a in addrs:
            u = a.get("uprn") or a.get("property_uprn")
            if not u:
                continue
            astr = " ".join(str(a.get(k, "")) for k in
                            ("address", "display_address", "line_1", "single_line_address",
                             "paon", "building_number", "building_name")).upper().strip()
            if ph and re.match(rf"{re.escape(ph)}\b", astr):
                uprn = str(u)
                break
        if not uprn and len(addrs) == 1:           # sparse postcode -> unambiguous
            u0 = addrs[0].get("uprn") or addrs[0].get("property_uprn")
            uprn = str(u0) if u0 else None
    except Exception as e:
        print(f"      epc address lookup failed ({postcode}): {e}")
        if _throttled(e):
            _kill("homedata", "429")
    result = {}
    if uprn:
        epc = (enrich_property(uprn) or {}).get("epc") or {}
        if epc.get("floor_area_m2"):
            result = {"floor_area_m2": epc["floor_area_m2"], "age_band": epc.get("age_band"),
                      "rating": epc.get("rating"), "uprn": uprn}
    if result.get("floor_area_m2"):
        conn.execute("INSERT OR REPLACE INTO epccache VALUES (?,?)", (key, json.dumps(result)))
        conn.commit()
    return result



# ===========================================================================
# EPC REGISTER (free, official) - floor area, property type, BUILT FORM.
# Replaces the paid Homedata enrichment. Two calls: search -> certificate.
# ===========================================================================
_EPC_FLOOR_KEYS = ("total_floor_area", "total-floor-area", "totalFloorArea",
                   "floor_area", "habitable_floor_area", "total_floor_area_m2")
_EPC_FORM_KEYS = ("built_form", "built-form", "builtForm")
_EPC_TYPE_KEYS = ("property_type", "property-type", "propertyType", "dwelling_type")
_EPC_AGE_KEYS = ("construction_age_band", "construction-age-band", "constructionAgeBand")
_EPC_BAND_KEYS = ("current_energy_efficiency_band", "current-energy-rating",
                  "currentEnergyEfficiencyBand", "energy_rating")


def _dig(obj, keys):
    """Find the first matching key anywhere in a nested dict (schemas vary by version)."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = _dig(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _dig(v, keys)
            if r is not None:
                return r
    return None


_EPC_LOGGED = [0]


def _epc_get(path, params):
    import requests
    if not EPC_API_KEY:
        print("      epc: no EPC_API_KEY visible - skipping")
        return None
    if _dead("epc"):
        return None
    url = EPC_BASE + path
    try:
        r = requests.get(url, params=params, timeout=25,
                         headers={"Authorization": f"Bearer {EPC_API_KEY}",
                                  "Accept": "application/json", "User-Agent": _UA})
        if _EPC_LOGGED[0] < 3:          # verbose for the first few, then quiet
            _EPC_LOGGED[0] += 1
            print(f"      epc {path} {params} -> HTTP {r.status_code}; "
                  f"body starts: {r.text[:200]!r}")
        if r.status_code == 429:
            _kill("epc", "429")
            return None
        if r.status_code in (401, 403):
            print(f"      epc AUTH problem HTTP {r.status_code} - check the token value")
            _kill("epc", str(r.status_code))
            return None
        if r.status_code == 404:
            return None                  # genuinely no certificate for this query
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"      epc {path} failed: {e}")
        if _throttled(e):
            _kill("epc", "429")
        return None



def _norm_addr(t):
    """Uppercase, strip punctuation, collapse spaces - for address comparison."""
    t = re.sub(r"[^A-Z0-9 ]", " ", (t or "").upper())
    return re.sub(r"\s+", " ", t).strip()


_ADDR_STOP = {"THE", "OF", "AND", "ROAD", "RD", "LANE", "LN", "CLOSE", "DRIVE",
              "AVENUE", "AVE", "STREET", "ST", "WAY", "COURT", "HOUSE", "COTTAGE",
              "FARNHAM", "SURREY", "HAMPSHIRE", "DECEASED", "LATE"}


def _addr_match_score(epc_line, lead_addr):
    """0-1 similarity between an EPC address line and a probate/listing address.
    House numbers must agree when both sides have one; otherwise token overlap."""
    a = set(_norm_addr(epc_line).split())
    b = set(_norm_addr(lead_addr).split())
    if not a or not b:
        return 0.0
    na = {t for t in a if t.isdigit()}
    nb = {t for t in b if t.isdigit()}
    if na and nb and not (na & nb):
        return 0.0                      # different house numbers - definitely not it
    if bool(na) != bool(nb):
        # One side is numbered, the other isn't: we cannot prove it's the same house,
        # and matching on street name alone would attach a NEIGHBOUR's certificate.
        return 0.30                     # deliberately below the 0.45 match threshold
    # Score on NAME tokens only - a shared house number must never carry a match on
    # its own, or "4 Brock Close" would match "4 Cedarways".
    aw = (a - _ADDR_STOP) - na
    bw = (b - _ADDR_STOP) - nb
    if not aw or not bw:
        aw, bw = a - na, b - nb
    if not aw or not bw:
        return 1.0 if (na and nb and (na & nb)) else 0.0
    overlap = len(aw & bw) / min(len(aw), len(bw))
    if na and nb and (na & nb):
        overlap = min(1.0, overlap + 0.35)   # matching house number corroborates
    return overlap

def epc_lookup(conn, postcode, paon, addr_hint="", budget=None):
    """Postcode + house number -> the property's EPC record (free, official register).
    Gives floor area, property type and BUILT FORM (detached/semi/terrace) - the last
    of which is what lets us drop semis reliably instead of guessing from the address.
    `budget` (a mutable [int], optional) caps live search calls per run: cache hits are
    always free; a live lookup is only made while budget remains, then it's decremented."""
    if not EPC_ENABLED or not postcode:
        return {}
    key = f"EPC|{postcode}|{paon}|{(addr_hint or '')[:40]}".upper()
    row = conn.execute("SELECT data FROM epccache WHERE k=?", (key,)).fetchone()
    if row:
        try:
            return json.loads(row["data"])
        except Exception:
            return {}
    if _dead("epc"):
        return {}
    if budget is not None:
        if budget[0] <= 0:
            return {}
        budget[0] -= 1
    data = _epc_get("/api/domestic/search", {"postcode": postcode})
    certs = (data or {}).get("data") or []
    if isinstance(certs, dict):
        certs = [certs]
    if not certs:
        return {}
    target = (addr_hint or paon or "").strip()
    best, best_score = None, 0.0
    for c in certs:
        line = " ".join(str(c.get(k) or "") for k in
                        ("addressLine1", "addressLine2", "addressLine3", "addressLine4"))
        sc = _addr_match_score(line, target)
        if sc > best_score or (sc == best_score and best is not None
                               and str(c.get("registrationDate", "")) > str(best.get("registrationDate", ""))):
            best, best_score = c, sc
    pick = best if best_score >= 0.45 else None
    if pick is None and len(certs) == 1:
        # Single-certificate postcode: only accept when we can PROVE it's the same
        # dwelling. Safe cases: neither side carries a house number (nothing can
        # contradict), OR both carry a number AND those numbers actually match.
        # Never accept when both are numbered but the numbers DIFFER - that would
        # attach a neighbour's certificate (wrong floor area / built form / value).
        one = certs[0]
        cert_nums = set(re.findall(r"\d+", str(one.get("addressLine1") or "")))
        lead_nums = set(re.findall(r"\d+", _norm_addr(target)))
        if (not cert_nums and not lead_nums) or (cert_nums and lead_nums and (cert_nums & lead_nums)):
            pick = one
    if pick is None:
        if _EPC_LOGGED[0] <= 3:
            sample = [str(c.get("addressLine1") or "") for c in certs[:4]]
            print(f"      epc no address match for {target!r} in {postcode} "
                  f"(best {best_score:.2f}); certs: {sample}")
        return {}
    num = pick.get("certificateNumber") or pick.get("certificate_number")
    full = _epc_get("/api/certificate", {"certificate_number": num}) if num else None
    body = (full or {}).get("data") or {}
    fa = _dig(body, _EPC_FLOOR_KEYS)
    try:
        fa = int(round(float(fa))) if fa is not None else None
    except Exception:
        fa = None
    out = {"floor_area_m2": fa,
           "built_form": _as_text(_dig(body, _EPC_FORM_KEYS), _BUILT_FORM_CODES),
           "property_type": _as_text(_dig(body, _EPC_TYPE_KEYS), _PROP_TYPE_CODES),
           "age_band": _dig(body, _EPC_AGE_KEYS) or "",
           "rating": (pick.get("currentEnergyEfficiencyBand")
                      or _dig(body, _EPC_BAND_KEYS) or ""),
           "uprn": pick.get("uprn") or _dig(body, ("uprn",)) or "",
           "certificate": num or ""}
    if _EPC_LOGGED[0] <= 3:
        print(f"      epc matched {target!r} -> cert {pick.get('addressLine1')!r} | "
              f"floor={out['floor_area_m2']} "
              f"form={out['built_form']!r} type={out['property_type']!r} "
              f"(raw form={_dig(body, _EPC_FORM_KEYS)!r} "
              f"raw type={_dig(body, _EPC_TYPE_KEYS)!r}) (cert {num})")
        if body:
            print(f"      epc certificate keys: {sorted(body)[:40]}")
    if out["floor_area_m2"] or out["built_form"]:
        conn.execute("INSERT OR REPLACE INTO epccache VALUES (?,?)", (key, json.dumps(out)))
        conn.commit()
    return out


def _canon_pc(pc):
    """Canonical UK postcode (upper, single space before the 3-char inward code) so a
    subject's noisy postcode matches the bulk EPC file's keys (built the same way)."""
    s = re.sub(r"\s+", "", (pc or "").upper())
    return s[:-3] + " " + s[-3:] if len(s) >= 5 else s


def subject_epc(conn, postcode, paon, addr_hint="", budget=None):
    """Floor area / built form / type / UPRN for a SUBJECT property. Prefers the local
    bulk EPC file (free, offline, EXACT postcode+house-number match) and only calls the
    live register on a miss - cutting register calls and speeding runs once the bulk file
    is present, and (with uprn_coords) letting a subject be precisely located offline.
    Same return shape as epc_lookup, so callers are unchanged. The exact key match means
    it can never attach a neighbour's certificate."""
    pc, hn = (postcode or "").strip(), (paon or "").strip()
    if pc and hn:
        e = _load_epc_local().get(f"{_canon_pc(pc)}|{hn}".upper())
        # Only short-circuit the live lookup when the bulk file ALSO gives a UPRN - the
        # UPRN is what drives precise (offline) geocoding, so a bulk file lacking it must
        # not suppress the live call, or subjects would silently lose their precise plot.
        # Comparables read the bulk file directly (they need only floor area), so a
        # UPRN-less bulk file still fully powers size-matched valuation.
        if e and e.get("fa") and e.get("uprn"):
            return {"floor_area_m2": e.get("fa"),
                    "built_form": e.get("form") or "",
                    "property_type": e.get("type") or "",
                    "age_band": "",
                    "rating": e.get("rating") or "",
                    "uprn": e.get("uprn") or "",
                    "certificate": ""}
    return epc_lookup(conn, postcode, paon, addr_hint, budget=budget)


# RdSAP stores built form / property type as numeric codes in the raw certificate.
# Standard SAP enumeration - used for LABELLING and (where confident) exclusion.
_BUILT_FORM_CODES = {1: "Detached", 2: "Semi-Detached", 3: "End-Terrace",
                     4: "Mid-Terrace", 5: "Enclosed End-Terrace",
                     6: "Enclosed Mid-Terrace"}
_PROP_TYPE_CODES = {0: "House", 1: "Bungalow", 2: "Flat", 3: "Maisonette",
                    4: "Park home"}


def _as_text(v, codes):
    """Certificates return either text or an RdSAP numeric code - normalise to text."""
    if v is None or v == "":
        return ""
    if isinstance(v, str) and not v.strip().isdigit():
        return v.strip()
    try:
        return codes.get(int(float(v)), "")
    except (TypeError, ValueError):
        return str(v)


def _form_is_excluded(built_form, prop_type):
    """EPC built form is the reliable way to drop semis/terraces. Type-safe:
    the raw certificate may hand us an int, a numeric string, or plain text."""
    f = str(built_form or "").lower()
    t = str(prop_type or "").lower()
    if any(k in t for k in ("flat", "maisonette", "park home")):
        return True
    return any(k in f for k in ("semi-detached", "semi detached", "mid-terrace",
                                "mid terrace", "end-terrace", "end terrace", "enclosed"))

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def _parse_dmy(s):
    """'14 March 2026' / '3rd February 2026' -> date."""
    m = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", (s or "").strip())
    if not m:
        return None
    mon = _MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(1)))
    except ValueError:
        return None


def _add_months(d0, n):
    t = d0.month - 1 + n
    return date(d0.year + t // 12, t % 12 + 1, 1)


def market_window(dod, notice_iso, today):
    """Estimated time-to-market from probate averages (death->market ~6-12 months;
    Gazette notice is post-grant, so notice->market ~1-6 months). Returns a phrase."""
    lo = hi = None
    if dod:
        lo, hi = _add_months(dod, 6), _add_months(dod, 12)
    elif notice_iso:
        try:
            nd = date.fromisoformat(notice_iso[:10])
            lo, hi = _add_months(nd, 1), _add_months(nd, 6)
        except Exception:
            pass
    if not lo:
        return None
    if today < lo:
        return f"Est. on market {lo:%b %Y}\u2013{hi:%b %Y}"
    if today <= hi:
        return f"Likely to market soon (by ~{hi:%b %Y})"
    return "May be on market now \u2014 check"


def fetch_probate_leads(conn):
    """Deceased Estates notices from The Gazette near home - the legal way to
    see an estate (often an empty house) before it reaches the market.
    One location-filtered query; free, no key, Open Government Licence."""
    if not PROBATE_ENABLED:
        return []
    import requests
    since = (datetime.now(timezone.utc).date()
             - timedelta(days=PROBATE_LOOKBACK_DAYS)).isoformat()
    params = {"location-postcode-1": PROBATE_LOCATION,
              "location-distance-1": PROBATE_RADIUS_MI,
              "start-publish-date": since,
              "results-page-size": 100, "sort-by": "latest-date"}
    url = "https://www.thegazette.co.uk/wills-and-probate/notice/data.json"
    if _dead("gazette"):
        return []
    try:
        r = requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"   probate fetch failed: {e}")
        if _throttled(e):
            _kill("gazette", "429/403")   # parks the notice-page fetches too (same host)
        return []

    entries = data.get("entry") or []
    _notice_fetches = [0]
    if isinstance(entries, dict):
        entries = [entries]

    raw = []
    for e in entries:
        content = _strip_tags(e.get("content") or "")
        pc = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", content)
        if not pc:
            continue
        pc_up = pc.group(1).upper()
        if pc_up.split()[0] in EXCLUDE_DISTRICTS:      # drop Aldershot/Fleet before any fetch
            continue
        pid = (e.get("id") or "").rsplit("/", 1)[-1]
        _lnk = e.get("link")
        notice_url = ""
        if isinstance(_lnk, dict):
            notice_url = _lnk.get("@href") or _lnk.get("href") or ""
        elif isinstance(_lnk, list):
            for _L in _lnk:
                if isinstance(_L, dict) and _L.get("@rel", "alternate") == "alternate":
                    notice_url = _L.get("@href") or _L.get("href") or ""
                    if notice_url:
                        break
        elif isinstance(_lnk, str):
            notice_url = _lnk
        if not notice_url:
            notice_url = f"https://www.thegazette.co.uk/notice/{pid}"
        dod = re.search(r"(?:who )?died on\s+(?:the\s+)?"
                        r"(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+\d{4})", content, re.I)
        # Prefer address-specific anchors; only fall back to a bare "of", which can match
        # too early (e.g. "estate of NAME deceased ... of 43 ...") and swallow the name.
        # Then strip leading noise so the HOUSE NUMBER, not "Deceased", anchors the line.
        addr_line = ""
        _PC = r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}"
        for _pat in (r"(?:lately residing at|residing at|late of|formerly of)\s+(.+?),?\s*" + _PC,
                     r"\bof\s+(.+?),?\s*" + _PC):
            _m = re.search(_pat, content, re.I)
            if _m:
                addr_line = _m.group(1).strip(" ,")
                break
        addr_line = re.sub(r"^(?:the\s+)?(?:late|deceased|of|and)\b[\s,]*", "",
                           addr_line, flags=re.I).strip(" ,")
        if _is_carehome(addr_line + " " + content):
            continue
        if _is_excluded_locality(addr_line + " " + content):   # Hale/Badshot Lea/etc - drop pre-fetch
            continue
        if _addr_is_flat(addr_line):
            continue
        paon_m = re.search(r"\b(\d+[A-Za-z]?)\b", addr_line)   # first house number anywhere, not only leading
        contact = _probate_contact(content, pc_up)
        sample = content[:900]
        if not contact.get("name") and _notice_fetches[0] < NOTICE_DETAIL_CAP:
            _notice_fetches[0] += 1
            detail = _fetch_notice_text(notice_url)
            if detail:
                if _is_carehome(detail):        # named as a care home only on the notice page
                    continue
                c2 = _probate_contact(detail, pc_up)
                if c2.get("name"):
                    contact = c2
                sample = detail[:900]
        raw.append({"id": f"gz_{pid}",
                    "name": _probate_name(e.get("title"), content),
                    "postcode": pc_up,
                    "paon": paon_m.group(1) if paon_m else "",
                    "addr_line": addr_line,
                    "contact": contact,
                    "notice_sample": sample,
                    "dod": dod.group(1) if dod else "",
                    "pub": (e.get("published") or "")[:10],
                    "url": notice_url})
    if not raw:
        return []
    _res = sum(1 for x in raw if (x.get("contact") or {}).get("name"))
    print(f"- probate: {len(raw)} notices, {_res} estate contacts resolved, "
          f"{_notice_fetches[0]} notice-page lookups")

    cache = load_geocache(conn)
    need = sorted({x["postcode"] for x in raw if x["postcode"] not in cache})
    fresh = geocode(need) if need else {}
    save_geocache(conn, fresh)
    coords = {**cache, **fresh}

    today = datetime.now(timezone.utc).date().isoformat()
    out = []
    for x in raw:
        ll = coords.get(x["postcode"])
        if not ll:
            continue
        lat, lng = ll
        epc = subject_epc(conn, x["postcode"], x.get("paon"), x.get("addr_line", ""))
        # (No Homedata EPC fallback: Homedata is reserved for LIVE LISTINGS only - its
        #  ~100/month quota is protected. The free EPC register is the enrichment source.)
        # precise UPRN coordinate (from the EPC certificate) beats the postcode centroid,
        # so the plot/footprint match below is this dwelling's own parcel, not the estate's
        _pll = uprn_coord(epc.get("uprn"))
        precise = bool(_pll)
        if _pll:
            lat, lng = _pll
        fa = epc.get("floor_area_m2")
        if _form_is_excluded(epc.get("built_form"), epc.get("property_type")):
            print(f"   dropped {x.get('addr_line') or x['postcode']} "
                  f"- EPC says {epc.get('built_form') or epc.get('property_type')}")
            continue                                   # real semi/terrace/flat - drop it
        _bf = str(epc.get("built_form") or "").lower()
        epc_type = ("detached" if ("detached" in _bf and "semi" not in _bf) else
                    "semi" if "semi" in _bf else
                    "terraced" if "terrace" in _bf else None)
        # THE HUMAN PROCESS: named comparable sales first (same street where
        # possible), own-sale anchor / area band as fallback. Local file - never blocked.
        ctx, local_comps = find_comps(x["postcode"], x.get("paon"), epc_type,
                                      x.get("addr_line", ""),
                                      subject_fa=epc.get("floor_area_m2"), conn=conn)
        if not ctx.get("est_mid"):
            ctx = price_context_local(x["postcode"], x.get("paon"), epc_type)
            local_comps = []
        basis = ctx.get("basis", "postcode")
        # The old live HM Land Registry Price Paid fallbacks (fetch_price_paid /
        # fetch_price_paid_town) were removed here: that API 403-blocks cloud IPs, so it
        # must NEVER be called from GitHub Actions (a reinforced IP block is fatal).
        # price_context_local above already widens sector -> district from the LOCAL
        # price_paid_region.json.gz (the supported bulk route); a postcode with no local
        # history simply stays unpriced (kept as "Long-held" when PROBATE_KEEP_UNKNOWN).
        typ = ctx.get("subject_type")
        est = ctx.get("est_mid")
        # interest filter - only trust micro-local (postcode/house) data to EXCLUDE;
        # a town-wide median is too coarse to reject a specific house on.
        reliable = (basis == "postcode")
        if reliable and typ and PROBATE_TYPES and typ not in PROBATE_TYPES:
            continue
        if est is not None and (est > PROBATE_MAX_PRICE or est < PROBATE_MIN_PRICE):
            continue
        if est is None and not PROBATE_KEEP_UNKNOWN:
            continue
        reasons = []
        win = market_window(_parse_dmy(x["dod"]), x["pub"], date.today())
        if win:
            reasons.append(win)
        if fa:
            reasons.append(f"{(typ or 'home').title()} \u00b7 {fa} m\u00b2")
        if ctx.get("est_mid"):
            lbl = "area est" if basis == "postcode" else f"{PROBATE_LOCATION}-area est"
            reasons.append(f"~\u00a3{ctx['est_low']//1000}k\u2013\u00a3{ctx['est_high']//1000}k {lbl}")
        elif not fa:
            reasons.append("Long-held \u2014 not sold since 1995")
        reasons = reasons[:3]
        out.append({
            "id": x["id"],
            "address": (f"{x['addr_line']} \u2014 {x['postcode']}" if x.get("addr_line")
                        else f"Estate of {x['name']} \u2014 {x['postcode']}"),
            "owner_note": f"Probate \u2014 estate of {x['name']}",
            "estate_contact": x.get("contact") or {},
            "notice_sample": ("" if (x.get("contact") or {}).get("name") else x.get("notice_sample","")),
            "postcode": x["postcode"], "price": ctx.get("est_mid") or 0, "beds": 0,
            "property_type": (epc.get("built_form") or epc.get("property_type")
                              or ctx.get("subject_type") or "property"), "lat": lat, "lng": lng,
            "precise_location": precise,
            "dist_mi": round(_haversine_mi(HOME, (lat, lng)), 1),
            "score": 68, "reasons": reasons[:3], "flags": ["probate"],
            "is_probate": True, "low_comp": False, "comps": [],
            "streetview_url": _streetview_url(lat, lng), "floor_area_m2": fa,
            "built_form": epc.get("built_form", ""), "epc_rating": epc.get("rating", ""),
            "uprn": epc.get("uprn", ""),
            "market_window": win, "est_low": ctx.get("est_low"), "est_high": ctx.get("est_high"),
            "est_mid": ctx.get("est_mid"),
            "local_comps": local_comps[:6],
            "est_basis": ctx.get("basis", ""), "est_n": ctx.get("n_comps"),
            "est_basis_type": ctx.get("basis_type", ""),
            "source": {"name": "The Gazette", "url": x["url"], "uprn": ""},
            "source_label": "PROBATE",
            "enrichment": {"market": {}, "plot": {}, "equity": {}},
            "media": {"photo_count": 0, "has_floorplan": False,
                      "thumb_url": _aerial_thumb(lat, lng)},
            "first_seen": today, "last_seen": today, "days_on_market": 0,
        })
    return out


def _combine(*lists):
    seen = {}
    for lst in lists:
        for p in (lst or []):
            seen[p["id"]] = p
    # collapse the same physical property (same address + postcode) to its best-scored entry
    out = {}
    for p in seen.values():
        key = re.sub(r"[^a-z0-9]", "", (p.get("address", "") + "|" + p.get("postcode", "")).lower())
        if not key or key == "|":
            key = "id:" + p["id"]
        cur = out.get(key)
        if cur is None or p.get("score", 0) > cur.get("score", 0):
            out[key] = p
    return sorted(out.values(), key=lambda x: -x.get("score", 0))


# ===========================================================================
# PRECISE LOCATION (OS Open UPRN, optional local file)
# Postcode-centroid geocoding often lands in the wrong ENCLOSING parcel, which is
# the single biggest plot/footprint accuracy bug. When we can resolve a property's
# UPRN (from its EPC certificate or a portal feed) to a precise coordinate, we snap
# the property onto it so parcel_for matches its OWN plot. Absent file -> no-op:
# everything falls back to centroid geocoding exactly as before.
# ===========================================================================
_UPRN = None


def _load_uprn():
    """Optional OS Open UPRN file: {uprn: [lat, lng]} (built by make_uprn.py, uploaded
    like the other local bulk files). Absent -> {} and the tool degrades gracefully."""
    global _UPRN
    if _UPRN is not None:
        return _UPRN
    try:
        with gzip.open(UPRN_FILE, "rt") as f:
            _UPRN = json.load(f).get("uprns", {})
        print(f"- loaded {len(_UPRN):,} UPRN coordinates for precise location")
    except Exception:
        _UPRN = {}
    return _UPRN


def uprn_coord(uprn):
    """Precise (lat, lng) for a UPRN, or None if unknown / no local file."""
    if not uprn:
        return None
    v = _load_uprn().get(str(uprn))
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return (v[0], v[1])
    return None


def _apply_precise_location(p, uprn):
    """Snap a property onto its precise UPRN coordinate and mark it precise, so the
    plot/footprint match is trusted rather than flagged approximate. Re-centres the
    distance-from-home, aerial thumb and Street View link. Returns True if it snapped."""
    ll = uprn_coord(uprn)
    if not ll:
        return False
    lat, lng = ll
    p["lat"], p["lng"] = lat, lng
    p["precise_location"] = True
    p["dist_mi"] = round(_haversine_mi(HOME, (lat, lng)), 1)
    media = p.get("media")
    if isinstance(media, dict):
        media["thumb_url"] = _aerial_thumb(lat, lng)
    if p.get("streetview_url"):
        p["streetview_url"] = _streetview_url(lat, lng)
    return True


def _listing_want_type(portal_type, epc_built_form):
    """Comparable property type for the local Price Paid file. EPC built form is
    authoritative and wins when present; otherwise fall back to the portal label.
    Bungalow / cottage / land stay UNCONSTRAINED (None) - Price Paid has no bungalow
    class, so forcing a type there would wrongly starve the comp pool."""
    bf = str(epc_built_form or "").lower()
    if bf:
        if "detached" in bf and "semi" not in bf:
            return "detached"
        if "semi" in bf:
            return "semi"
        if "terrace" in bf:
            return "terraced"
    t = (portal_type or "").lower()
    if "detached" in t:
        return "detached"
    return None


def enrich_listings(conn, props, budget):
    """Give public for-sale listings the SAME comps + EPC evidence probate leads get,
    so both stocks compete under one scoring lens (compare-to-home floor area, and
    value-vs-comparables rather than raw cheapness). Comps come from the local Price
    Paid file (free, never blocked); the subject EPC is a live call, budget-capped and
    circuit-broken. Best-effort and safe: a numberless listing that can't be matched to
    a certificate simply gets comps without a floor area - never a neighbour's cert.
    Writes the same keys the frontend already renders for probate leads."""
    enriched = valued = 0
    for p in props:
        pc = (p.get("postcode") or "").strip()
        if not pc:
            continue
        addr_line = (p.get("street") or (p.get("address") or "").split(",")[0]).strip()
        paon_m = re.search(r"\b(\d+[A-Za-z]?)\b", addr_line)
        paon = paon_m.group(1) if paon_m else ""
        epc = subject_epc(conn, pc, paon, addr_line, budget=budget) or {}
        # snap onto the precise UPRN coordinate if we have one (EPC cert or portal feed),
        # so the plot/footprint match is this dwelling's own, not the enclosing parcel
        _apply_precise_location(p, epc.get("uprn") or (p.get("source") or {}).get("uprn"))
        want_type = _listing_want_type(p.get("property_type"), epc.get("built_form"))
        fa = epc.get("floor_area_m2")
        # local, free comps (same routine as probate); size-matched only when a subject
        # floor area is known AND the bulk EPC file supplies comp floor areas (no live calls).
        ctx, comps = find_comps(pc, paon, want_type, addr_line, subject_fa=fa, conn=conn)
        if not ctx.get("est_mid"):
            ctx = price_context_local(pc, paon, want_type)
            comps = []
        if fa:
            p["floor_area_m2"] = fa
        if epc.get("built_form"):
            p["built_form"] = epc["built_form"]
        if epc.get("rating"):
            p["epc_rating"] = epc["rating"]
        if epc.get("uprn"):
            p["uprn"] = epc["uprn"]
        if fa or epc.get("built_form"):
            enriched += 1
        if ctx.get("est_mid"):
            p["est_low"] = ctx.get("est_low")
            p["est_mid"] = ctx.get("est_mid")
            p["est_high"] = ctx.get("est_high")
            p["est_basis"] = ctx.get("basis", "")
            p["est_n"] = ctx.get("n_comps")
            p["est_basis_type"] = ctx.get("basis_type", "")
            p["local_comps"] = (comps or [])[:6]
            valued += 1
    print(f"- listings enriched: {valued} with a comp estimate, {enriched} with EPC "
          f"(EPC budget left {budget[0]}/{LISTING_EPC_CAP})")
    return props


def agent_to_property(rec):
    """Map a fetch_agents.py record ({address,postcode,price,type,link,...}) into the
    property shape the engine scores, so agent-website listings run through the SAME
    geocode -> gate -> enrich -> score pipeline as everything else (one lens).
    Returns None if there's nothing usable."""
    # decode HTML entities the scrapers leave behind ("St John&#39;s", "Oak &amp; Elm")
    addr = html.unescape((rec.get("address") or "").strip())
    postcode = (rec.get("postcode") or "").strip().upper()
    if not addr and not postcode:
        return None
    # drop non-property pages the crawler sometimes grabs (listing indexes, archives,
    # search-result shells) - these have a title but describe no actual home
    _al = addr.lower()
    if (_al in ("property", "properties", "for sale", "search results")
            or "archive" in _al or "search result" in _al or "page not found" in _al):
        return None
    link = rec.get("link") or ""
    price = int(rec.get("price") or 0)
    t = html.unescape((rec.get("type") or "")).lower()
    ptype = ("land" if ("land" in t or "plot" in t) else
             "barn" if "barn" in t else
             "bungalow" if "bungalow" in t else
             "cottage" if "cottage" in t else
             "farm" if "farm" in t else
             "detached" if ("detached" in t and "semi" not in t) else
             "flat" if ("flat" in t or "apartment" in t or "maisonette" in t) else
             "semi" if "semi" in t else
             "terrace" if "terrac" in t else
             "house" if ("house" in t or "home" in t or "residence" in t or "property" in t) else
             (t.split("/")[0].strip() if t and t.split("/")[0].strip().isalpha() else "house"))
    pid = "ag_" + hashlib.sha1((link or (addr + postcode)).lower().encode()).hexdigest()[:10]
    reductions = 1 if rec.get("price_reduced") else 0
    # Distinguish a deliberately-unpriced listing (price on application) from a real
    # figure. Without this a POA listing shows as "£0" and reads as broken.
    price_poa = (not price) and (str(rec.get("price_qualifier") or "").upper() == "POA")
    return {
        "id": pid, "address": addr or postcode, "postcode": postcode,
        "street": addr.split(",")[0].strip() if addr else "",
        "lat": None, "lng": None,
        "property_type": ptype, "beds": int(rec.get("beds") or 0), "price": price,
        "price_poa": price_poa,
        "status": "live", "streetview_url": "",
        "source": {"portal": "agent", "name": rec.get("source") or "Local agent",
                   "url": link, "agent": rec.get("source") or "", "uprn": ""},
        "source_label": "AGENT",
        "media": {"photo_count": 0, "has_floorplan": False, "thumb_url": ""},
        "description_raw": "",
        "enrichment": {"market": {"reductions": reductions,
                                  "is_reduced": bool(rec.get("price_reduced")),
                                  "status": rec.get("status"), "dom": None,
                                  "added_date": rec.get("first_seen")}},
        "comps": [],
    }


def fetch_agent_leads():
    """Local estate-agent website listings via fetch_agents.py, converted to the
    engine's property shape. Fully guarded: any import/fetch failure returns [] so an
    agent site can never break the nightly run (fetch_agents has its own robots +
    crawl-delay + per-domain circuit-breaker)."""
    if not AGENTS_ENABLED or USE_MOCK:
        return []
    try:
        from fetch_agents import fetch_agent_listings
        raw = fetch_agent_listings() or []
    except Exception as e:
        print(f"- agent fetcher unavailable/failed: {e}")
        return []
    out = [p for p in (agent_to_property(r) for r in raw) if p]
    print(f"- {len(out)} agent-website listing(s) from {len({p['source'].get('name') for p in out})} agent(s)")
    return out


def main():
    print("PropertyScout run starting" + ("  [MOCK]" if USE_MOCK else "  [LIVE]"))
    print(f"- keys visible to this run: HOMEDATA={'yes' if HOMEDATA_API_KEY else 'MISSING'}"
          f" | EPC={'yes' if EPC_API_KEY else 'MISSING'}"
          + (f" (len {len(EPC_API_KEY)})" if EPC_API_KEY else ""))
    conn = db_connect()
    # purge any sample/mock records that leaked in before the API key was set
    purged = conn.total_changes
    conn.execute("DELETE FROM properties WHERE id LIKE 'hd\\_%' ESCAPE '\\'")
    conn.execute("DELETE FROM price_history WHERE id LIKE 'hd\\_%' ESCAPE '\\'")
    conn.execute("DELETE FROM geocache WHERE postcode IN ('GU10 4AH','GU35 8PN')")
    conn.commit()
    if conn.total_changes - purged:
        print(f"- purged {conn.total_changes - purged} stale sample record(s)")

    auctions = fetch_auction_lots(conn)
    print(f"- {len(auctions)} auction lot(s) within {AUCTION_RADIUS_MI} mi of home")

    probate = fetch_probate_leads(conn)
    print(f"- {len(probate)} probate lead(s) within {PROBATE_RADIUS_MI} mi of {PROBATE_LOCATION}")

    agent_leads = fetch_agent_leads()

    rows = fetch_listings()
    print(f"- {len(rows)} Homedata listing(s) fetched")

    props = []
    for row in rows:
        p = listing_to_property(row)
        if p:
            props.append(p)
    props.extend(agent_leads)   # agent-website listings run through the same pipeline
    print(f"- {len(props)} listing(s) to process "
          f"({len(props) - len(agent_leads)} Homedata + {len(agent_leads)} agent)")

    if not props:
        print("  !! 0 listings from any source (Homedata quota + no agent leads).")
        saved = recover_from_db(conn)
        combined = _combine(saved, auctions, probate)
        if combined:
            _publish(combined, conn)
            print(f"  !! Republished {len(saved)} cached + {len(auctions)} auction lot(s).")
        else:
            print("  !! No cached data to fall back to; leaving existing file untouched.")
        conn.close(); return

    # map pins from postcodes (free)
    seed_geocache_from_properties(conn)
    cache = load_geocache(conn)
    need = sorted({p["postcode"] for p in props if p["postcode"] and p["postcode"] not in cache})
    fresh = geocode(need) if need else {}
    save_geocache(conn, fresh)
    coords = {**cache, **fresh}
    for p in props:
        ll = coords.get(p["postcode"]) or _MOCK_COORDS.get(p["postcode"])
        if ll:
            p["lat"], p["lng"] = ll
    have = sum(1 for p in props if p["lat"] is not None)
    print(f"- coords {have}/{len(props)} (cached {len(cache)}, fresh {len(fresh)})")

    # keep only what falls inside the hand-drawn target patch
    inside = []
    for p in props:
        if p["lat"] is not None and _in_polygon(p["lat"], p["lng"], AREA_POLYGON):
            p["dist_mi"] = round(_haversine_mi(HOME, (p["lat"], p["lng"])), 1)
            p["media"]["thumb_url"] = _aerial_thumb(p["lat"], p["lng"])
            inside.append(p)
    props = inside
    print(f"- {len(props)} inside target area")

    # same lens as probate: comps + subject EPC over the public listings, so both
    # stocks are scored on the same evidence (compare-to-home, value-vs-comparables)
    enrich_listings(conn, props, [LISTING_EPC_CAP])

    # score everything cheaply
    for p in props:
        p["enrichment"].update(_plot_for(p))
        score_property(p)
    props.sort(key=lambda x: -x["score"])

    # spend credits only on the top shortlist that has a uprn (paid reveal flow)
    enriched = 0
    for p in props:
        if enriched >= ENRICH_TOP_N:
            break
        uprn = p["source"].get("uprn")
        if not uprn:
            continue
        extra = enrich_property(uprn)
        p["comps"] = extra.pop("comps", []) or p["comps"]
        p["enrichment"].update(extra)
        score_property(p)
        enriched += 1
    print(f"- enriched {enriched} (uprn needed; free tier withholds it)")

    props.sort(key=lambda x: -x["score"])
    for p in props:
        p["flags"] = detect_flags(p)
        build_reasons(p)
        upsert(conn, p)

    print("- top results:")
    for p in props[:15]:
        m = p["enrichment"].get("market") or {}
        tag = f"{m.get('reductions',0)}red {m.get('dom',0)}d"
        print(f"   {p['score']:>3}  GBP{p['price']:>7,}  {p['address'][:34]:34}  {p['property_type'][:9]:9} {tag}")

    if not props:
        saved = recover_from_db(conn)
        combined = _combine(saved, auctions, probate)
        if combined:
            _publish(combined, conn)
            print(f"  !! 0 fresh after filtering - republished {len(saved)} cached + {len(auctions)} auction.")
        else:
            print("  !! 0 properties and no cache - leaving existing file untouched.")
        conn.close(); return
    final = _combine(props, auctions, probate)
    _publish(final, conn)
    print(f"- published {len(props)} listings + {len(auctions)} auction + {len(probate)} probate = {len(final)} total")
    conn.close()


def _plot_for(p):
    return {"plot": _MOCK_PLOT.get(p["postcode"], {})}


# ===========================================================================
# MOCK DATA
# ===========================================================================
def _mock_listings():
    return [
        {"id": "hd_0001", "street": "Beech Hill Road", "postcode": "GU10 4AH",
         "property_uprn": "100061234567", "latest_price": 649000, "bedrooms": 3,
         "property_type": "Detached Bungalow", "latest_status": "Reduced",
         "days_on_market": 61, "times_reduced": 2, "is_reduced": True,
         "agent_name": "Smiths Estates", "added_date": "2026-04-20"},
        {"id": "hd_0002", "street": "School Lane", "postcode": "GU35 8PN",
         "property_uprn": "100061234890", "latest_price": 795000, "bedrooms": 3,
         "property_type": "Detached", "latest_status": "Reduced",
         "days_on_market": 104, "times_reduced": 3, "is_reduced": True,
         "agent_name": "Rural Property Co", "added_date": "2026-03-08"},
    ]


_MOCK_EPC = {
    "100061234567": {"floor_area_m2": 110, "rating": "E", "age_band": "1950-1966"},
    "100061234890": {"floor_area_m2": 95, "rating": "F", "age_band": "before 1900"},
}
_MOCK_COMPS = {
    "100061234567": [{"price": 790000, "date": "2025-11", "m2": 118, "renovated": True},
                     {"price": 835000, "date": "2025-09", "m2": 132, "renovated": True}],
    "100061234890": [{"price": 1050000, "date": "2025-08", "m2": 160, "renovated": True},
                     {"price": 980000, "date": "2025-10", "m2": 145, "renovated": True}],
}
_MOCK_COORDS = {"GU10 4AH": (51.196, -0.847), "GU35 8PN": (51.115, -0.830)}
_MOCK_PLOT = {
    "GU10 4AH": {"area_acres": 0.45, "source": "inspire"},
    "GU35 8PN": {"area_acres": 1.2, "source": "inspire"},
}

if __name__ == "__main__":
    main()
