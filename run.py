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
import os, re, json, gzip, sqlite3, hashlib, time, math
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
HOME_PLOT_M2 = 380                       # measured plot area (m2) from title plan SY519861
HOME_PLOT_OUTLINE = [[-5.27, -19.32], [4.58, -19.32], [6.07, 19.32], [-5.38, 19.32]]  # plot shape, centred metres
PLOTS_FILE = Path(__file__).resolve().parent / "plots_waverley.json.gz"  # HMLR INSPIRE parcels
FOOTPRINTS_FILE = Path(__file__).resolve().parent / "footprints_bowl.json.gz"  # OS OpenMap Local buildings
MIN_PLOT_M2 = HOME_PLOT_M2               # gate: lead plot must be >= your home plot
DETACHED_ONLY = True                     # gate: drop clear non-detached dwellings
EXCLUDE_DISTRICTS = {"GU11", "GU12", "GU14", "GU51", "GU52"}  # Aldershot/Farnborough/Fleet - out of area
NOTICE_DETAIL_CAP = 40                   # max per-notice page fetches per run (rate-limit guard)
_UA = "Mozilla/5.0 (compatible; PropertyScout/1.0)"

WEIGHTS = {"equity_residual": 20, "plot_size": 30, "structural": 25,
           "motivation": 20, "competition": 10, "location": 15}
RENO_RATE_PER_M2 = {"poor": 1200, "dated": 900, "fair": 600}
EXTENSION_ALLOWANCE = 40_000
LOW_COMP_THRESHOLD = 7

HOMEDATA_API_KEY = os.environ.get("HOMEDATA_API_KEY", "")
HOMEDATA_BASE = os.environ.get("HOMEDATA_BASE", "https://api.homedata.co.uk")
ENRICH = True       # floor area / EPC / comps - only fires when a uprn exists
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
    return None


def fetch_listings():
    if USE_MOCK or not HOMEDATA_API_KEY:
        return _mock_listings()
    import requests
    out = []
    for area in SEARCH["areas"]:
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
            continue
        print(f"   {area}: {len(rows)} listing(s)")
        out.extend(rows)
        time.sleep(0.4)
    return out


def geocode(postcodes):
    """Postcode -> (lat, lng) via postcodes.io bulk (free, no key)."""
    import requests
    out, uniq = {}, sorted({pc for pc in postcodes if pc})
    for i in range(0, len(uniq), 100):
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
        "id": pid, "address": addr, "postcode": postcode,
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


def score_property(p):
    sig, parts = {}, []
    epc = p["enrichment"].get("epc") or {}
    plot = p["enrichment"].get("plot") or {}
    market = p["enrichment"].get("market") or {}

    # equity - only if we have a comp + floor area (paid reveal)
    cap = WEIGHTS["equity_residual"]; comp = _renovated_comp(p); fa = epc.get("floor_area_m2")
    if comp and fa:
        reno = int(fa * _reno_rate(p))
        if p["property_type"] == "bungalow":
            reno += EXTENSION_ALLOWANCE
        gain = comp - p["price"] - reno
        p["enrichment"]["equity"] = {"renovated_comp": comp, "reno_cost_est": reno, "equity_gain": gain}
        pts = max(0, min(cap, round((gain / p["price"]) / 0.25 * cap)))
        sig["equity_residual"] = {"score": pts, "max": cap,
                                  "note": f"Renovated comp ~£{comp:,}; ~£{gain:,} gain after ~£{reno:,} works."}
        parts.append((pts, cap))

    # plot - only if known (paid reveal)
    cap = WEIGHTS["plot_size"]; acres = plot.get("area_acres")
    if acres is not None:
        pts = (cap if acres >= 1 else round(cap*0.87) if acres >= 0.5 else round(cap*0.8)
               if acres >= 0.4 else round(cap*0.67) if acres >= 0.25 else round(cap*0.47)
               if acres >= 0.15 else round(cap*0.3))
        sig["plot_size"] = {"score": pts, "max": cap, "note": f"Est. {acres:.2f} acre ({plot.get('source','est')})."}
        parts.append((pts, cap))

    # structural - always (type + any text signals)
    cap = WEIGHTS["structural"]; hits = _has(p["description_raw"], STRUCTURAL_TERMS)
    pts = round(cap * 0.4)
    if p["property_type"] in ("bungalow", "cottage"):
        pts += round(cap * 0.3)
    if p["property_type"] in ("farm", "land", "plot", "smallholding"):
        pts += round(cap * 0.4)
    pts += min(round(cap * 0.3), len(hits) * 5)
    pts = max(0, min(cap, pts))
    sig["structural"] = {"score": pts, "max": cap,
                         "note": (f"{p['property_type'].title()}. " +
                                  (f"Signals: {', '.join(hits)}." if hits else "Type-based potential."))}
    parts.append((pts, cap))

    # motivation - always (reductions / days-on-market / status)
    cap = WEIGHTS["motivation"]
    reductions = market.get("reductions") or 0
    dom = market.get("dom") or 0
    status = (market.get("status") or "").lower()
    hits = _has(p["description_raw"], MOTIVATION_TERMS)
    pts = min(round(cap*0.4), len(hits)*5) + min(round(cap*0.5), int(reductions)*5)
    if dom > 180:  pts += round(cap*0.4)
    elif dom > 90: pts += round(cap*0.25)
    elif dom > 60: pts += round(cap*0.15)
    pts = max(0, min(cap, pts))
    bits = []
    if reductions: bits.append(f"{reductions} reduction(s)")
    if dom: bits.append(f"{dom} days listed")
    if hits: bits.append("'" + "', '".join(hits) + "'")
    sig["motivation"] = {"score": pts, "max": cap, "note": "; ".join(bits) or "Fresh to market, no signals yet."}
    parts.append((pts, cap))

    # competition - always (stale stock = less competition)
    cap = WEIGHTS["competition"]; pts = 3
    if dom > 120: pts += 5
    elif dom > 75: pts += 3
    pts = max(0, min(cap, pts))
    sig["competition"] = {"score": pts, "max": cap, "note": "Lower competition the longer it sits."}
    parts.append((pts, cap))

    # location - only if we fetched school/station data (not on free tier yet)
    schools = p["enrichment"].get("schools") or []
    station = p["enrichment"].get("station_distance_mi")
    if schools or station is not None:
        cap = WEIGHTS["location"]; pts = 0
        outstanding = [s for s in schools if (s.get("rating") or "").lower() == "outstanding"]
        if outstanding: pts += round(cap*0.5)
        if station is not None: pts += round(cap * (0.5 if station <= 4 else 0.3))
        pts = max(0, min(cap, pts))
        sig["location"] = {"score": pts, "max": cap, "note": "School / station proximity."}
        parts.append((pts, cap))

    achieved = sum(pt for pt, _ in parts)
    possible = sum(c for _, c in parts) or 1
    p["signals"] = sig
    p["score"] = round(achieved / possible * 100)
    p["low_comp"] = sig.get("competition", {}).get("score", 0) >= LOW_COMP_THRESHOLD
    p["scored_at"] = datetime.now(timezone.utc).isoformat()
    return p


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
"""


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


def fetch_constraints(lat, lng):
    """Planning constraints at a point via planning.data.gov.uk (free, keyless).
    Returns {list, feasibility}. Feasibility is a 0.5-1.0 multiplier on the score."""
    if lat is None or lng is None:
        return {}
    key = (round(lat, 5), round(lng, 5))
    if key in _CONSTRAINT_CACHE:
        return _CONSTRAINT_CACHE[key]
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
        return {}
    res = {"list": [_CONSTRAINT_LABEL[d] for d in found], "datasets": found, "grade": grade}
    _CONSTRAINT_CACHE[key] = res
    return res


def fetch_planning_history(lat, lng, krad=0.5):
    """Local planning approval rate via PlanIt (free, keyless). Returns approvals vs
    refusals within krad km, last ~12 years - real precedent for 'will they permit'."""
    if lat is None or lng is None:
        return {}
    key = (round(lat, 4), round(lng, 4))
    if key in _PLANIT_CACHE:
        return _PLANIT_CACHE[key]
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
    if plot:
        ratio = plot / home
        potential = min(1.0, 0.25 + 0.22 * ratio)
        pot_note = f"{plot:,} m\u00b2 plot ({ratio:.1f}\u00d7 your home)"
    else:
        ratio, potential, pot_note = 0, 0.35, "plot unverified"
    fb = p.get("footprints") or {}
    cov = fb.get("coverage_pct")
    if plot and cov is not None and cov <= 12:
        potential = min(1.0, potential + 0.08)
        pot_note += f" · {cov}% built (room to develop)"
    price = p.get("price") or p.get("est_mid")
    if price:
        value = 1.0 - _band01(price, PROBATE_MIN_PRICE, PROBATE_MAX_PRICE)
        lo, hi = p.get("est_low"), p.get("est_high")
        if lo and hi:
            value = min(1.0, value + 0.15 * _band01((hi - lo) / price, 0, 0.5))
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
    core = 0.45 * potential + 0.30 * value + 0.25 * fit
    p["score"] = int(round(min(100, (core * 85 + edge * 100) * feas)))
    if ratio >= 2.0:
        typ = "Plot play"
    elif price and value >= 0.7:
        typ = "Anomaly"
    elif edge >= 0.10:
        typ = "Motivated seller"
    elif fit >= 0.7 and ratio >= 1.0:
        typ = "Forever-fit"
    else:
        typ = "Candidate"
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
        "Potential": {"score": round(potential * 45), "max": 45, "note": pot_note},
        "Value": {"score": round(value * 30), "max": 30, "note": val_note},
        "Fit": {"score": round(fit * 25), "max": 25, "note": fit_note},
        "Edge": {"score": round(edge * 100), "max": 20, "note": edge_note},
    }
    if perm is not None:
        ph = p.get("planning") or {}
        note = f"{p.get('permission_label','')}"
        if ph.get("decided"):
            note += f" · {ph['approved']} approved / {ph['refused']} refused nearby"
        sig["Permission"] = {"score": round(perm * 20), "max": 20, "note": note.strip(" ·")}
    p["signals"] = sig
    return p


def apply_gates(props):
    """Stamp plot size; apply detached + plot-size hard gates (keep unverified plots)."""
    out = []
    dropped_type = dropped_plot = dropped_area = 0
    for p in props:
        if p.get("lat") is not None and not _in_polygon(p["lat"], p["lng"], AREA_POLYGON):
            dropped_area += 1
            continue
        _pcd = (p.get("postcode") or "").split()[0].upper()
        if _pcd in EXCLUDE_DISTRICTS:
            dropped_area += 1
            continue
        if DETACHED_ONLY and (_excluded_type(p.get("property_type")) or _addr_is_flat(p.get("address"))):
            dropped_type += 1
            continue
        lat, lng = p.get("lat"), p.get("lng")
        parcel = parcel_for(lat, lng)
        area = parcel["a"] if parcel else None
        p["plot_m2"] = area
        if area is not None and area < MIN_PLOT_M2:
            dropped_plot += 1
            continue
        if area is None:
            fl = p.setdefault("flags", [])
            if "plot-unverified" not in fl:
                fl.append("plot-unverified")
        fb = analyze_buildings(parcel)
        if fb:
            p["footprints"] = fb
            if fb.get("main_m2") and not p.get("floor_area_m2"):
                p["floor_area_est_m2"] = round(fb["main_m2"] * 2 * 0.9)
        con = fetch_constraints(lat, lng)
        if con:
            p["constraints"] = con["list"]
            if con.get("grade"):
                p["listed_grade"] = con["grade"]
            ph = fetch_planning_history(lat, lng)
            if ph:
                p["planning"] = ph
            perm = permission_estimate(con.get("datasets"), ph)
            p["permission"] = perm["estimate"]
            p["permission_label"] = perm["label"]
            p["feasibility"] = max(0.4, perm["estimate"])
        p["setting"] = _local_density(lat, lng)
        score_property(p)
        out.append(p)
    if dropped_type or dropped_plot or dropped_area:
        print(f"- gates: dropped {dropped_area} out-of-area, {dropped_type} non-detached, {dropped_plot} under {MIN_PLOT_M2} m2 plot")
    return out


def _publish(props):
    props = apply_gates(props)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(),
         "count": len(props), "home_floor_m2": HOME_FLOOR_AREA_M2,
         "home_plot_m2": HOME_PLOT_M2, "min_plot_m2": MIN_PLOT_M2,
         "home_plot_outline": HOME_PLOT_OUTLINE,
         "properties": props}, indent=2))


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
                 (p["id"], p["first_seen"], p["last_seen"], p["price"], json.dumps(p)))
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
    if not AUCTION_ENABLED:
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
    if not AUCTION_ENABLED:
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
    _res = sum(1 for x in raw if (x.get("contact") or {}).get("name"))
    print(f"- probate: {len(raw)} notices, {_res} estate contacts resolved, "
          f"{_notice_fetches[0]} notice-page lookups")

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


def fetch_auctionhouse_lots(conn):
    """Auction House (Sussex & Hampshire): available lots, postcode-geocoded.
    Templated network site, so this parser extends to their other regions."""
    if not AUCTION_ENABLED:
        return []
    import requests
    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml",
               "Accept-Language": "en-GB,en;q=0.9"}
    try:
        r = requests.get(AUCTIONHOUSE_URL, headers=headers, timeout=30)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"   auctionhouse fetch failed: {e}")
        return []

    raw = []
    for m in re.finditer(r'<a[^>]+href="([^"]*/lot/(?:redirect/)?\d+)"[^>]*>(.*?)</a>',
                         html, re.S):
        url_l, inner = m.group(1), _strip_tags(m.group(2))
        u = inner.upper()
        if "PROPERTY FOR AUCTION" not in u or "GUIDE" not in u:
            continue  # "GUIDE" marks an available lot; sold/withdrawn lack it
        pc = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", inner)
        if not pc:
            continue
        addr_m = re.search(r"-\s*(.+?)\s+Lot\b", inner)
        bed_m = re.search(r"(\d+)\s*Bed", inner)
        lot_m = re.search(r"(\d+)$", url_l)
        raw.append({"url": url_l, "text": inner,
                    "postcode": pc.group(1).upper().replace("  ", " "),
                    "price": _first_price(inner) or 0,
                    "beds": int(bed_m.group(1)) if bed_m else 0,
                    "addr": addr_m.group(1).strip() if addr_m else pc.group(1),
                    "lotid": lot_m.group(1) if lot_m else pc.group(1)})
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
        dist = _haversine_mi(HOME, (lat, lng))
        if dist > AUCTION_RADIUS_MI:
            continue
        ptype, _ = _infer_type_beds(x["text"])
        out.append({
            "id": f"ah_{x['lotid']}", "address": f"{x['addr']} (auction)",
            "postcode": x["postcode"], "price": x["price"], "beds": x["beds"],
            "property_type": ptype, "lat": lat, "lng": lng,
            "dist_mi": round(dist, 1), "score": _auction_score(x["text"]),
            "reasons": _auction_reasons(x["text"], x["price"]), "flags": ["auction"],
            "is_auction": True, "low_comp": False, "comps": [],
            "source": {"name": "Auction House", "url": x["url"], "uprn": ""},
            "source_label": "AUCTION",
            "enrichment": {"market": {}, "plot": {}, "equity": {}},
            "media": {"photo_count": 0, "has_floorplan": False,
                      "thumb_url": _aerial_thumb(lat, lng)},
            "first_seen": today, "last_seen": today, "days_on_market": 0,
        })
    out.sort(key=lambda x: -x["score"])
    return out


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
    kind = "solicitor" if re.search(r"\b(Solicitors?|LLP|& Co|Legal|Law|Trust Corporation)\b",
                                    name, re.I) else "executor"
    return {"name": name[:60], "ref": ref, "postcode": contact_pc, "kind": kind}


_NOTICE_CACHE = {}


def _fetch_notice_text(url):
    """The search feed omits the executor block; the notice's own page carries it
    in labelled fields (Executor/Administrator Company Name, Postcode, Reference).
    Fetch that page and flatten to text. Cached; fails soft; logs HTTP status."""
    if not url:
        return ""
    if url in _NOTICE_CACHE:
        return _NOTICE_CACHE[url]
    txt = ""
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=20)
        if r.status_code != 200:
            print(f"   notice page HTTP {r.status_code}: {url}")
        if r.ok:
            txt = _strip_tags(r.text)
    except Exception as e:
        print(f"   notice fetch failed {url}: {e}")
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
    try:
        r = requests.get(url, params={**params, "_pageSize": 150, "_sort": "-transactionDate"},
                         headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        return _pp_parse(r.json())
    except Exception as e:
        print(f"   price-paid fetch failed ({params}): {e}")
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


def _streetview_url(lat, lng):
    return f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lng}"


def homedata_epc(conn, postcode, paon):
    """Floor area (m2) for a property via Homedata: postcode -> UPRN -> EPC.
    Reuses your existing key; cached so each property is fetched at most once."""
    if not HOMEDATA_API_KEY or USE_MOCK:
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
    try:
        r = requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"   probate fetch failed: {e}")
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
        addr_m = re.search(r"(?:late of|residing at|formerly of|of)\s+(.+?),?\s*"
                           r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", content)
        addr_line = (addr_m.group(1).strip(" ,") if addr_m else "")
        if re.search(r"(nursing home|care home|rest home|residential home|"
                     r"retirement home|convalescent|care centre|nursing centre|"
                     r"nursing|care of|c/o|hospital|hospice|almshouse|"
                     r"sheltered|extra care|assisted living)",
                     (addr_line + " " + content), re.I):
            continue
        if _addr_is_flat(addr_line):
            continue
        paon_m = re.match(r"([0-9]+[A-Za-z]?)\b", addr_line)
        contact = _probate_contact(content, pc_up)
        sample = content[:900]
        if not contact.get("name") and _notice_fetches[0] < NOTICE_DETAIL_CAP:
            _notice_fetches[0] += 1
            detail = _fetch_notice_text(notice_url)
            if detail:
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

    pp_cache = {}
    town_cache = {}
    today = datetime.now(timezone.utc).date().isoformat()
    out = []
    for x in raw:
        ll = coords.get(x["postcode"])
        if not ll:
            continue
        lat, lng = ll
        if x["postcode"] not in pp_cache:
            pp_cache[x["postcode"]] = fetch_price_paid(x["postcode"])
        ctx = price_context(pp_cache[x["postcode"]], x.get("paon"))
        basis = "postcode"
        # fallback: postcode has no Land Registry history -> coarse town-wide estimate
        if not ctx.get("est_mid"):
            if PROBATE_LOCATION not in town_cache:
                town_cache[PROBATE_LOCATION] = fetch_price_paid_town(PROBATE_LOCATION)
            tctx = price_context(town_cache[PROBATE_LOCATION], None)
            if tctx.get("est_mid"):
                ctx["est_low"], ctx["est_high"] = tctx["est_low"], tctx["est_high"]
                ctx["est_mid"], ctx["n_comps"] = tctx["est_mid"], tctx["n_comps"]
                basis = "town"
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
        epc = homedata_epc(conn, x["postcode"], x.get("paon"))
        fa = epc.get("floor_area_m2")
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
            "property_type": ctx.get("subject_type") or "property", "lat": lat, "lng": lng,
            "dist_mi": round(_haversine_mi(HOME, (lat, lng)), 1),
            "score": 68, "reasons": reasons[:3], "flags": ["probate"],
            "is_probate": True, "low_comp": False, "comps": [],
            "streetview_url": _streetview_url(lat, lng), "floor_area_m2": fa,
            "market_window": win, "est_low": ctx.get("est_low"), "est_high": ctx.get("est_high"),
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


def main():
    print("PropertyScout run starting" + ("  [MOCK]" if USE_MOCK else "  [LIVE]"))
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

    rows = fetch_listings()
    print(f"- {len(rows)} listing(s) fetched")
    if not rows:
        print("  !! 0 listings - likely Homedata monthly quota exhausted or API error.")
        saved = recover_from_db(conn)
        combined = _combine(saved, auctions, probate)
        if combined:
            _publish(combined)
            print(f"  !! Republished {len(saved)} cached + {len(auctions)} auction lot(s).")
        else:
            print("  !! No cached data to fall back to; leaving existing file untouched.")
        conn.close(); return

    props = []
    for row in rows:
        p = listing_to_property(row)
        if p:
            props.append(p)
    print(f"- {len(props)} match target types (detached/bungalow/plot, no new-builds)")

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
            _publish(combined)
            print(f"  !! 0 fresh after filtering - republished {len(saved)} cached + {len(auctions)} auction.")
        else:
            print("  !! 0 properties and no cache - leaving existing file untouched.")
        conn.close(); return
    final = _combine(props, auctions, probate)
    _publish(final)
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


# mock enrichment keyed by the mock uprn (only used in USE_MOCK)
def _mock_listings_uprn_patch():
    pass


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
