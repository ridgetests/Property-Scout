"""
EPC enrichment — floor area, energy rating, age band.

The Energy Performance of Buildings register offers a free API (register for a
key at the official EPC open-data service; set EPC_API_KEY and EPC_API_EMAIL as
environment variables / Action secrets). Floor area is the closest structured
proxy you'll get to internal size, and feeds both the equity calc and the
"plot not tagged" logic.

STATUS: stub returns mock EPC data keyed by postcode so the pipeline runs.
Finish _fetch_live to call the real endpoint.
"""
from __future__ import annotations
import base64
from pipeline.models import Property
from adapters.base import EnrichmentAdapter
from config import EPC_API_KEY, EPC_API_EMAIL

USE_MOCK = True

_MOCK = {
    "GU10 4AH": {"floor_area_m2": 110, "rating": "E", "age_band": "1950-1966"},
    "GU35 8PN": {"floor_area_m2": 95,  "rating": "F", "age_band": "before 1900"},
}


class EPCAdapter(EnrichmentAdapter):
    name = "epc"

    def enrich(self, prop: Property) -> dict:
        if USE_MOCK:
            return {"epc": _MOCK.get(prop.postcode, {})}
        return {"epc": self._fetch_live(prop)}

    def _fetch_live(self, prop: Property) -> dict:
        """
        import requests
        token = base64.b64encode(f"{EPC_API_EMAIL}:{EPC_API_KEY}".encode()).decode()
        r = requests.get(
            "https://EPC_API_HOST/api/v1/domestic/search",      # TODO: real host
            params={"postcode": prop.postcode},
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
            timeout=20,
        )
        rows = r.json().get("rows", [])
        match = _best_address_match(rows, prop.address)         # TODO
        return {
            "floor_area_m2": int(float(match["total-floor-area"])),
            "rating": match["current-energy-rating"],
            "age_band": match["construction-age-band"],
        }
        """
        raise NotImplementedError("Finish _fetch_live, then set USE_MOCK=False")
