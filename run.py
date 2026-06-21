#!/usr/bin/env python3
"""
PropertyScout — single-file runner (Homedata live-listings).

Everything (config, scoring, storage, Homedata calls, pipeline) is in this ONE
file. Replace your repo's run.py with it. Only external dependency: requests.

Go live: set USE_MOCK = False below.
"""
from __future__ import annotations
import os, re, json, sqlite3, hashlib, time
from datetime import date, datetime, timezone
from pathlib import Path

# ===========================================================================
# CONFIG  — set False for real Homedata data once a mock run works
# ===========================================================================
USE_MOCK = False

SEARCH = {
    "max_price": 800_000,
    "min_price": 450_000,
    # Boundary NAMES — resolved to Homedata boundary_ids at run time.
    # Waverley covers GU8/9/10/27; East Hampshire covers GU35.
    "areas": ["Waverley", "East Hampshire"],
    "min_beds": 2,
}
WEIGHTS = {"equity_residual": 20, "plot_size": 30, "structural": 25,
           "motivation": 20, "competition": 10, "location": 15}
MAX_TOTAL = sum(WEIGHTS.values())
RENO_RATE_PER_M2 = {"poor": 1200, "dated": 900, "fair": 600}
EXTENSION_ALLOWANCE = 40_000
LOW_COMP_THRESHOLD = 7
THIN_CHANNELS = {"auction", "off-market"}

HOMEDATA_API_KEY = os.environ.get("HOMEDATA_API_KEY", "")
HOMEDATA_BASE = os.environ.get("HOMEDATA_BASE", "https://api.homedata.co.uk")
ENRICH = True   # set False to save API credits (skips floor area / EPC / comps)

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "scout.db"
OUT_PATH = ROOT / "docs" / "properties.json"

MOTIVATION_TERMS = ["executor", "probate", "estate of", "sold as seen",
                    "no onward chain", "no chain", "cash buyers", "deceased"]
STRUCTURAL_TERMS = ["planning permission", "pp granted", "development potential",
                    "scope to", "potential to", "annexe", "outbuilding",
                    "workshop", "barn", "in need of modernisation"]


def _headers():
    return {"Authorization": f"Api-Key {HOMEDATA_API_KEY}", "Accept": "application/json"}


# ===========================================================================
# HOMEDATA  (boundary lookup -> live listings -> per-property enrichment)
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
                         "m2": c.get("epc_floor_area"),
                         "renovated": None,
                         "distance_mi": round(c["distance_meters"] / 1609, 1)
                                        if c.get("distance_meters") else None}
                        for c in rows if c.get("sold_let_price")]
    except Exception as e:
        print(f"      comparables {uprn} failed: {e}")
    return out


def listing_to_property(row):
    price = int(row.get("latest_price") or row.get("price") or 0)
    if not price:
        return None
    addr = row.get("display_address") or row.get("address") or ""
    uprn = str(row.get("property_uprn") or row.get("uprn") or "")
    pid = uprn or hashlib.sha1(addr.lower().encode()).hexdigest()[:8]
    sub = (row.get("listing_property_type") or row.get("loki_property_type")
           or row.get("property_type") or "").lower()
    ptype = ("bungalow" if "bungalow" in sub else "detached" if "detached" in sub
             else "semi" if "semi" in sub else (sub or "house"))
    geo = row.get("geopoint") or {}
    dom = row.get("days_on_market")
    if dom is None and row.get("added_date"):
        try:
            dom = (date.today() - date.fromisoformat(str(row["added_date"])[:10])).days
        except Exception:
            dom = None
    reductions = row.get("times_reduced")
    if reductions is None:
        reductions = 1 if row.get("reduced_date") else 0
    return {
        "id": pid, "address": addr, "postcode": row.get("postcode") or "",
        "lat": geo.get("lat"), "lng": geo.get("lon") or geo.get("lng"),
        "property_type": ptype, "beds": int(row.get("bedrooms") or 0), "price": price,
        "status": "live", "relisted_count": 0,
        "source": {"portal": "homedata", "listing_id": str(row.get("id") or ""),
                   "url": "", "agent": row.get("agent_name") or "", "uprn": uprn},
        "media": {"photo_count": len(row.get("images") or []), "has_floorplan": False, "thumb_url": ""},
        "description_raw": row.get("description") or "",
        "enrichment": {"market": {"reductions": reductions,
                                  "status": row.get("latest_status"), "dom": dom}},
        "comps": [],
    }


# ===========================================================================
# SCORING
# ===========================================================================
def _has(text, terms):
    t = (text or "").lower()
    return [w for w in terms if w in t]


def _reno_rate(p):
    r = ((p["enrichment"].get("epc") or {}).get("rating") or "").upper()
    return (RENO_RATE_PER_M2["poor"] if r in ("F", "G") else
            RENO_RATE_PER_M2["dated"] if r in ("E", "D") else RENO_RATE_PER_M2["fair"])


def _renovated_comp(p):
    reno = sorted(c["price"] for c in p["comps"] if c.get("renovated"))
    if reno:
        return reno[len(reno) // 2]
    fa = (p["enrichment"].get("epc") or {}).get("floor_area_m2")
    ppm2 = sorted(c["price"] / c["m2"] for c in p["comps"] if c.get("m2"))
    if fa and ppm2:
        return int(ppm2[len(ppm2) // 2] * fa * 1.15)
    return None


def score_property(p):
    sig, total = {}, 0
    epc = p["enrichment"].get("epc") or {}
    plot = p["enrichment"].get("plot") or {}
    market = p["enrichment"].get("market") or {}

    cap = WEIGHTS["equity_residual"]; comp = _renovated_comp(p); fa = epc.get("floor_area_m2")
    if comp and fa:
        reno = int(fa * _reno_rate(p))
        if p["property_type"] == "bungalow" or _has(p["description_raw"], ["extend"]):
            reno += EXTENSION_ALLOWANCE
        gain = comp - p["price"] - reno
        p["enrichment"]["equity"] = {"renovated_comp": comp, "reno_cost_est": reno, "equity_gain": gain}
        pts = max(0, min(cap, round((gain / p["price"]) / 0.25 * cap)))
        note = f"Renovated comp ~£{comp:,}. After ~£{reno:,} works, est. £{gain:,} gain."
    else:
        pts, note = 0, "Insufficient comp / floor-area data."
    sig["equity_residual"] = {"score": pts, "max": cap, "note": note}; total += pts

    cap = WEIGHTS["plot_size"]; acres = plot.get("area_acres")
    if acres is None:
        pts, note = round(cap * 0.4), "Plot size unknown — manual check."
    else:
        pts = (cap if acres >= 1 else round(cap*0.87) if acres >= 0.5 else round(cap*0.8)
               if acres >= 0.4 else round(cap*0.67) if acres >= 0.25 else round(cap*0.47)
               if acres >= 0.15 else round(cap*0.3))
        note = f"Est. {acres:.2f} acre ({plot.get('source','est')})."
    sig["plot_size"] = {"score": pts, "max": cap, "note": note}; total += pts

    cap = WEIGHTS["structural"]; hits = _has(p["description_raw"], STRUCTURAL_TERMS)
    pts = round(cap * 0.45)
    if p["property_type"] == "bungalow":
        pts += round(cap * 0.2)
    pts += min(round(cap * 0.35), len(hits) * 4)
    pts = max(0, min(cap, pts))
    sig["structural"] = {"score": pts, "max": cap,
                         "note": ("Bungalow — extend-up case. " if p["property_type"] == "bungalow" else "")
                                 + (f"Signals: {', '.join(hits)}." if hits else "No text signals.")}
    total += pts

    cap = WEIGHTS["motivation"]; hits = _has(p["description_raw"], MOTIVATION_TERMS)
    reductions = market.get("reductions")
    if reductions is None:
        reductions = max(0, len(p.get("price_history", [])) - 1)
    dom = market.get("dom") or p.get("days_on_market", 0)
    status = (market.get("status") or "").lower()
    pts = min(round(cap*0.4), len(hits)*5) + min(round(cap*0.5), int(reductions)*5)
    if dom and dom > 90:   pts += round(cap*0.25)
    elif dom and dom > 60: pts += round(cap*0.15)
    if status in ("reduced", "withdrawn", "re-listed", "relisted"): pts += round(cap*0.1)
    pts = max(0, min(cap, pts))
    bits = []
    if hits: bits.append("'" + "', '".join(hits) + "'")
    if reductions: bits.append(f"{reductions} reduction(s)")
    if dom: bits.append(f"{dom} days on market")
    if status: bits.append(status)
    sig["motivation"] = {"score": pts, "max": cap, "note": "; ".join(bits) or "No motivated-seller signals."}
    total += pts

    cap = WEIGHTS["competition"]; media = p["media"]; pts = 0
    if (media.get("photo_count") or 0) < 6: pts += 3
    if not media.get("has_floorplan"): pts += 2
    if p["source"].get("portal") in THIN_CHANNELS: pts += 4
    if dom and dom > 75: pts += 2
    pts = max(0, min(cap, pts))
    sig["competition"] = {"score": pts, "max": cap, "note": "Low listing engagement / thin channel."}
    total += pts

    cap = WEIGHTS["location"]; schools = p["enrichment"].get("schools") or []
    station = p["enrichment"].get("station_distance_mi"); pts = 0
    outstanding = [s for s in schools if (s.get("rating") or "").lower() == "outstanding"]
    if outstanding:
        pts += round(cap * (0.5 if min(s["distance_mi"] for s in outstanding) <= 2 else 0.3))
    if station is not None:
        pts += round(cap * (0.5 if station <= 4 else 0.35 if station <= 8 else 0.2))
    pts = max(0, min(cap, pts))
    sig["location"] = {"score": pts, "max": cap,
                       "note": (f"{outstanding[0]['name']} Outstanding. " if outstanding else "")
                               + (f"{station:.1f}mi to station" if station is not None else "Limited location data")}
    total += pts

    p["signals"] = sig
    p["score"] = round(total / MAX_TOTAL * 100)
    p["low_comp"] = sig["competition"]["score"] >= LOW_COMP_THRESHOLD or \
                    p["source"].get("portal") in THIN_CHANNELS
    p["scored_at"] = datetime.now(timezone.utc).isoformat()
    return p


def detect_flags(p):
    flags = []; desc = (p["description_raw"] or "").lower(); media = p["media"]
    plot = p["enrichment"].get("plot") or {}
    if (media.get("photo_count") or 0) < 6: flags.append("few_photos")
    if not media.get("has_floorplan"): flags.append("no_floorplan")
    acres = plot.get("area_acres")
    if acres and acres >= 0.25 and not re.search(r"\b(acre|plot|grounds|paddock)\b", desc):
        flags.append("plot_not_tagged")
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
"""


def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA); return conn


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
    p["days_on_market"] = (date.today() - date.fromisoformat(p["first_seen"])).days
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
    rows = fetch_listings()
    print(f"- {len(rows)} listing(s) fetched")
    published = []
    for row in rows:
        p = listing_to_property(row)
        if not p:
            continue
        prev, prev_price = existing(conn, p["id"])
        if prev and prev_price == p["price"] and prev.get("enrichment", {}).get("epc"):
            p["enrichment"].update({k: v for k, v in prev["enrichment"].items() if k != "market"})
            p["comps"] = prev.get("comps", [])
        else:
            extra = enrich_property(p["source"].get("uprn"))
            p["comps"] = extra.pop("comps", []) or p["comps"]
            p["enrichment"].update(extra)
            p["enrichment"].update(_plot_for(p))
        score_property(p)
        p["flags"] = detect_flags(p)
        upsert(conn, p)
        eq = p["enrichment"].get("equity", {}).get("equity_gain", 0)
        print(f"   scored {p['address'][:34]:34} -> {p['score']}  (£{eq:,} equity)")
        published.append(p)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(),
         "count": len(published), "properties": sorted(published, key=lambda x: -x["score"])},
        indent=2))
    print(f"- published {len(published)} properties -> {OUT_PATH}")
    conn.close()


def _plot_for(p):
    # Homedata has no plot-size field; this is the free Land-Registry slot.
    # Mock plot keyed by postcode; live listings have no postcode so this is
    # empty until you wire the INSPIRE/title-boundary lookup (next step).
    return {"plot": _MOCK_PLOT.get(p["postcode"], {})}


# ===========================================================================
# MOCK DATA  (live-listings shape, so a mock run proves the pipeline)
# ===========================================================================
def _mock_listings():
    return [
        {"id": "hd_0001", "display_address": "Beech Hill Road, Rowledge", "postcode": "GU10 4AH",
         "property_uprn": "100061234567", "latest_price": 649000, "bedrooms": 3,
         "listing_property_type": "Detached Bungalow", "latest_status": "Reduced",
         "days_on_market": 61, "times_reduced": 2, "agent_name": "Smiths Estates",
         "geopoint": {"lat": 51.196, "lon": -0.847}, "description": ""},
        {"id": "hd_0002", "display_address": "School Lane, Headley", "postcode": "GU35 8PN",
         "property_uprn": "100061234890", "latest_price": 795000, "bedrooms": 3,
         "listing_property_type": "Detached", "latest_status": "Reduced",
         "days_on_market": 104, "times_reduced": 3, "agent_name": "Rural Property Co",
         "geopoint": {"lat": 51.118, "lon": -0.835}, "description": ""},
    ]


_MOCK_EPC = {
    "100061234567": {"floor_area_m2": 110, "rating": "E", "age_band": "1950-1966"},
    "100061234890": {"floor_area_m2": 95, "rating": "F", "age_band": "before 1900"},
}
_MOCK_COMPS = {
    "100061234567": [{"price": 790000, "date": "2025-11", "m2": 118, "renovated": True, "distance_mi": 0.3},
                     {"price": 835000, "date": "2025-09", "m2": 132, "renovated": True, "distance_mi": 0.6}],
    "100061234890": [{"price": 1050000, "date": "2025-08", "m2": 160, "renovated": True, "distance_mi": 0.9},
                     {"price": 980000, "date": "2025-10", "m2": 145, "renovated": True, "distance_mi": 1.2}],
}
_MOCK_PLOT = {
    "GU10 4AH": {"area_acres": 0.45, "source": "inspire"},
    "GU35 8PN": {"area_acres": 1.2, "source": "inspire"},
}

if __name__ == "__main__":
    main()
