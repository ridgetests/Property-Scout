"""
EPC enrichment — floor area, energy rating, construction age band.

These feed the equity calc (floor area drives the renovation cost estimate)
and the "plot not tagged" logic.

-- SERVICE TRANSITION (important) ------------------------------------------
The long-standing API at epc.opendatacommunities.org was retired at the end of
May 2026 and replaced by the "Get energy performance of buildings data" service:

    https://get-energy-performance-data.communities.gov.uk

Register there with GOV.UK One Login to get your credentials. The new service
issues a Bearer token; the legacy service used HTTP Basic (email:api-key,
base64-encoded). This adapter supports BOTH via EPC_AUTH_MODE so you can switch
without touching the parsing logic.

CONFIRM the exact search path on the guidance page when you register and set
EPC_BASE_URL accordingly — the field names below (total-floor-area,
current-energy-rating, construction-age-band) are stable across the migration,
so only the endpoint + auth need checking.
----------------------------------------------------------------------------
"""
from __future__ import annotations
import os
import re
import time
import base64
from pipeline.models import Property
from adapters.base import EnrichmentAdapter

USE_MOCK = True   # flip to False once credentials + endpoint are confirmed

# -- endpoint / auth config -------------------------------------------------
# New service (confirm exact /search path on the guidance page after sign-up):
EPC_BASE_URL = os.environ.get(
    "EPC_BASE_URL",
    "https://get-energy-performance-data.communities.gov.uk/api/v1",
)
EPC_AUTH_MODE = os.environ.get("EPC_AUTH_MODE", "bearer")   # "bearer" | "basic"
EPC_BEARER_TOKEN = os.environ.get("EPC_BEARER_TOKEN", "")
EPC_API_KEY = os.environ.get("EPC_API_KEY", "")
EPC_API_EMAIL = os.environ.get("EPC_API_EMAIL", "")

# Representative year per construction age band, for a rough condition proxy.
AGE_BAND_YEAR = {
    "before 1900": 1890, "1900-1929": 1915, "1930-1949": 1940,
    "1950-1966": 1958, "1967-1975": 1971, "1976-1982": 1979,
    "1983-1990": 1987, "1991-1995": 1993, "1996-2002": 1999,
    "2003-2006": 2005, "2007-2011": 2009, "2012 onwards": 2015,
}

_MOCK = {
    "GU10 4AH": {"floor_area_m2": 110, "rating": "E", "age_band": "1950-1966"},
    "GU35 8PN": {"floor_area_m2": 95,  "rating": "F", "age_band": "before 1900"},
}


class EPCAdapter(EnrichmentAdapter):
    name = "epc"

    def _auth_header(self) -> dict:
        if EPC_AUTH_MODE == "bearer":
            return {"Authorization": f"Bearer {EPC_BEARER_TOKEN}"}
        token = base64.b64encode(f"{EPC_API_EMAIL}:{EPC_API_KEY}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def enrich(self, prop: Property) -> dict:
        if USE_MOCK:
            return {"epc": _MOCK.get(prop.postcode, {})}
        return {"epc": self._fetch_live(prop)}

    def _fetch_live(self, prop: Property) -> dict:
        import requests
        if not prop.postcode:
            return {}
        headers = {"Accept": "application/json", **self._auth_header()}
        try:
            r = requests.get(
                f"{EPC_BASE_URL}/domestic/search",
                params={"postcode": prop.postcode, "size": 100},
                headers=headers, timeout=20,
            )
            r.raise_for_status()
            rows = r.json().get("rows", [])
        except Exception as e:
            print(f"      EPC lookup failed for {prop.postcode}: {e}")
            return {}
        time.sleep(0.5)   # be gentle

        match = _best_address_match(rows, prop.address)
        if not match:
            return {}
        try:
            fa = float(match.get("total-floor-area") or 0)
        except (TypeError, ValueError):
            fa = 0
        return {
            "floor_area_m2": int(fa) if fa else None,
            "rating": (match.get("current-energy-rating") or "").upper(),
            "age_band": match.get("construction-age-band") or "",
            "age_year": AGE_BAND_YEAR.get(match.get("construction-age-band", "").lower()),
        }


def _norm(s: str) -> set:
    return set(re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).split())


def _best_address_match(rows: list, address: str) -> dict | None:
    """Pick the EPC row whose address best overlaps the listing address."""
    want = _norm(address)
    if not want or not rows:
        return rows[0] if rows else None
    best, best_score = None, 0
    for row in rows:
        row_addr = " ".join(str(row.get(k, "")) for k in ("address", "address1", "address2"))
        score = len(want & _norm(row_addr))
        if score > best_score:
            best, best_score = row, score
    return best or rows[0]
