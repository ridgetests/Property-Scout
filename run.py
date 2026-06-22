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
import os, re, json, sqlite3, hashlib, time, math
from datetime import date, datetime, timezone
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
    (51.216, -0.895), (51.211, -0.858), (51.208, -0.815), (51.214, -0.778),
    (51.224, -0.756), (51.180, -0.742), (51.150, -0.748), (51.122, -0.770),
    (51.120, -0.820), (51.128, -0.855), (51.165, -0.878), (51.198, -0.888),
]

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


def _publish(props):
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(),
         "count": len(props), "properties": props}, indent=2))


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
    rows = fetch_listings()
    print(f"- {len(rows)} listing(s) fetched")
    if not rows:
        print("  !! 0 listings - likely Homedata monthly quota exhausted or API error.")
        saved = recover_from_db(conn)
        if saved:
            _publish(saved)
            print(f"  !! Republished {len(saved)} cached properties from last good run.")
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
        if saved:
            _publish(saved)
            print(f"  !! 0 fresh after filtering - republished {len(saved)} cached instead.")
        else:
            print("  !! 0 properties and no cache - leaving existing file untouched.")
        conn.close(); return
    _publish(props)
    print(f"- published {len(props)} properties -> {OUT_PATH}")
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
