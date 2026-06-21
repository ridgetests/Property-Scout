"""
Homedata enrichment adapter — floor area, EPC and comparables via UPRN.

Chains off the UPRN the source adapter captured for each listing:
  GET /properties/{uprn}   -> floor area, EPC rating, construction age, last sale
  GET /comparables?uprn=.. -> nearby comps for the equity calc (2 calls each)

Endpoints (per Homedata docs):
  property record host:  https://homedata.co.uk/api/v1/properties/{uprn}
  comparables host:      https://api.homedata.co.uk/api/comparables  (legacy host)
Confirm exact comparables path/params in the playground — the property record
shape (epc_floor_area, current_energy_rating, construction_age_band, ...) is
confirmed from their docs.

Credit note: enrichment is the expensive part. The runner only calls this for
NEW or price-changed listings (see run.py), so steady-state credit use stays low.
"""
from __future__ import annotations
import os
import time
import requests
from pipeline.models import Property
from adapters.base import EnrichmentAdapter

USE_MOCK = True   # flip to False alongside the source adapter

API_KEY = os.environ.get("HOMEDATA_API_KEY", "")
BASE = os.environ.get("HOMEDATA_BASE", "https://homedata.co.uk/api/v1")
LEGACY_BASE = os.environ.get("HOMEDATA_LEGACY_BASE", "https://api.homedata.co.uk/api")
HEADERS = {"Authorization": f"Api-Key {API_KEY}", "Accept": "application/json"}
FETCH_COMPARABLES = True   # set False to save 2 calls/property if not needed

_MOCK_PROP = {
    "100061234567": {"epc_floor_area": 110, "current_energy_rating": "E",
                     "construction_age_band": "1950-1966"},
    "100061234890": {"epc_floor_area": 95, "current_energy_rating": "F",
                     "construction_age_band": "before 1900"},
}
_MOCK_COMPS = {
    "100061234567": [
        {"price": 790000, "date": "2025-11", "m2": 118, "renovated": True, "distance_mi": 0.3},
        {"price": 835000, "date": "2025-09", "m2": 132, "renovated": True, "distance_mi": 0.6},
    ],
    "100061234890": [
        {"price": 1050000, "date": "2025-08", "m2": 160, "renovated": True, "distance_mi": 0.9},
        {"price": 980000, "date": "2025-10", "m2": 145, "renovated": True, "distance_mi": 1.2},
    ],
}


class HomedataEnrichmentAdapter(EnrichmentAdapter):
    name = "homedata_enrich"

    def enrich(self, prop: Property) -> dict:
        uprn = (prop.source or {}).get("uprn")
        if not uprn:
            return {}
        if USE_MOCK or not API_KEY:
            rec = _MOCK_PROP.get(uprn, {})
            out = {"epc": _epc_from(rec)}
            if FETCH_COMPARABLES:
                out["comps"] = _MOCK_COMPS.get(uprn, [])
            return out
        return self._fetch_live(uprn)

    def _fetch_live(self, uprn: str) -> dict:
        out = {}
        try:
            r = requests.get(f"{BASE}/properties/{uprn}", headers=HEADERS, timeout=20)
            r.raise_for_status()
            out["epc"] = _epc_from(r.json())
        except Exception as e:
            print(f"      Homedata property {uprn} failed: {e}")
        time.sleep(0.3)
        if FETCH_COMPARABLES:
            try:
                rc = requests.get(f"{LEGACY_BASE}/comparables/{uprn}/",
                                  params={"count": 20}, headers=HEADERS, timeout=20)
                rc.raise_for_status()
                rows = rc.json().get("comparables") or rc.json().get("data") or []
                out["comps"] = [_comp(c) for c in rows if c]
            except Exception as e:
                print(f"      Homedata comparables {uprn} failed: {e}")
        return out


def _epc_from(rec: dict) -> dict:
    fa = rec.get("epc_floor_area") or rec.get("internal_area_sqm")
    return {
        "floor_area_m2": int(fa) if fa else None,
        "rating": (rec.get("current_energy_rating") or "").upper(),
        "age_band": rec.get("construction_age_band") or "",
    }


def _comp(c: dict) -> dict:
    price = int(c.get("sold_let_price") or c.get("price") or c.get("sold_price") or 0)
    dist_m = c.get("distance_meters")
    return {
        "price": price,
        "date": c.get("sold_date") or c.get("date") or "",
        "m2": c.get("epc_floor_area") or c.get("floor_area") or c.get("internal_area_sqm"),
        "renovated": c.get("renovated"),
        "distance_mi": round(dist_m / 1609, 1) if dist_m else c.get("distance_mi"),
    }
