"""
Land Registry enrichment — comparable sold prices, and a plot-size proxy.

Two free Land Registry sources:
  * Price Paid Data (bulk CSV): every sold price in England & Wales. Download
    once, load locally, query by postcode district for comps. Your true-value
    backbone. Mark a comp as renovated using a simple £/m² threshold or manual
    tagging once you've eyeballed a few.
  * INSPIRE Index Polygons (GeoJSON): registered land boundaries → plot area.
    Free download per local authority; intersect the property point with the
    polygon to estimate acreage.

STATUS: stub returns mock comps + plot so the pipeline runs end-to-end.
"""
from __future__ import annotations
from pipeline.models import Property
from adapters.base import EnrichmentAdapter

USE_MOCK = True

_MOCK_COMPS = {
    "GU10": [
        {"price": 790000, "date": "2025-11", "m2": 118, "renovated": True,  "distance_mi": 0.3},
        {"price": 835000, "date": "2025-09", "m2": 132, "renovated": True,  "distance_mi": 0.6},
        {"price": 610000, "date": "2025-12", "m2": 104, "renovated": False, "distance_mi": 0.4},
    ],
    "GU35": [
        {"price": 1050000, "date": "2025-08", "m2": 160, "renovated": True, "distance_mi": 0.9},
        {"price": 980000,  "date": "2025-10", "m2": 145, "renovated": True, "distance_mi": 1.2},
    ],
}
_MOCK_PLOT = {
    "GU10 4AH": {"area_acres": 0.45, "source": "inspire", "confidence": 0.8},
    "GU35 8PN": {"area_acres": 1.20, "source": "inspire", "confidence": 0.9},
}


class LandRegistryAdapter(EnrichmentAdapter):
    name = "landregistry"

    def enrich(self, prop: Property) -> dict:
        if USE_MOCK:
            district = prop.postcode.split()[0] if prop.postcode else ""
            return {
                "comps": _MOCK_COMPS.get(district, []),
                "plot": _MOCK_PLOT.get(prop.postcode, {}),
            }
        return self._fetch_live(prop)

    def _fetch_live(self, prop: Property) -> dict:
        """
        comps = query_price_paid(prop.postcode, prop.property_type)   # local CSV
        plot  = plot_from_inspire(prop.lat, prop.lng)                 # GeoJSON intersect
        return {"comps": comps, "plot": plot}
        """
        raise NotImplementedError("Finish _fetch_live, then set USE_MOCK=False")
