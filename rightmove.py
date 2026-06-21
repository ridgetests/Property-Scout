"""
Rightmove source adapter.

Robust approach: rather than brittle CSS selectors, Rightmove embeds a JSON
model in its search results page. We extract that blob and read the properties
array from it — far more stable across their frequent markup changes.

-- BEFORE YOU ENABLE (read) ------------------------------------------------
  * Rightmove's terms prohibit scraping. This is for personal, low-volume use.
    Keep the rate slow, cache, and run off-peak. Hammering the site gets you
    blocked and pushes you toward paid proxies.
  * This is the FRAGILE layer by design. If Rightmove changes the embedded
    model or blocks you, drop in a paid listings API as a replacement adapter
    (same interface) without touching anything downstream.

-- TWO THINGS TO FINISH ----------------------------------------------------
  1. LOCATION_IDS: Rightmove searches by an internal locationIdentifier, not by
     postcode text. Resolve each of your outcodes ONCE: do a normal search on
     rightmove.co.uk for e.g. "GU10", then copy the locationIdentifier value
     from the resulting URL (looks like OUTCODE^1234) into the dict below.
  2. _extract_model / field keys: confirm the embedded-model variable name and
     the property field names against one live page (print MODEL_RE matches),
     then set USE_MOCK = False. The mapping is centralised in _map_property.
----------------------------------------------------------------------------
"""
from __future__ import annotations
import re
import json
import time
import random
from pipeline.models import RawListing
from adapters.base import SourceAdapter

USE_MOCK = True   # flip to False once LOCATION_IDS + field keys are confirmed

# Fill these once by copying locationIdentifier from a manual Rightmove search URL.
LOCATION_IDS = {
    "GU10": "OUTCODE^FILL_ME",
    "GU9":  "OUTCODE^FILL_ME",
    "GU35": "OUTCODE^FILL_ME",
    "GU8":  "OUTCODE^FILL_ME",
    "GU27": "OUTCODE^FILL_ME",
}

BASE = "https://www.rightmove.co.uk/property-for-sale/find.html"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1"),
    "Accept-Language": "en-GB,en;q=0.9",
}
# The embedded model assignment. Confirm the variable name on a live page; both
# of these have been used by Rightmove at different times.
MODEL_RE = re.compile(r"window\.(?:jsonModel|PAGE_MODEL)\s*=\s*(\{.*?\});", re.DOTALL)
PAGE_SIZE = 24


class RightmoveAdapter(SourceAdapter):
    name = "rightmove"
    cost = "free"

    def fetch(self, criteria: dict) -> list[RawListing]:
        if USE_MOCK:
            return _mock_listings()
        out = []
        for area in criteria.get("areas", []):
            loc = LOCATION_IDS.get(area)
            if not loc or "FILL_ME" in loc:
                print(f"      skip {area}: no locationIdentifier set")
                continue
            out.extend(self._fetch_area(area, loc, criteria))
            time.sleep(random.uniform(4, 8))   # polite gap between areas
        return out

    def _fetch_area(self, area: str, loc: str, criteria: dict) -> list[RawListing]:
        import requests
        listings, index = [], 0
        while True:
            params = {
                "locationIdentifier": loc,
                "maxPrice": criteria.get("max_price"),
                "minPrice": criteria.get("min_price"),
                "minBedrooms": criteria.get("min_beds"),
                "propertyTypes": "detached,bungalow",
                "index": index,
                "sortType": 6,           # newest first
            }
            try:
                resp = requests.get(BASE, params=params, headers=HEADERS, timeout=20)
                resp.raise_for_status()
            except Exception as e:
                print(f"      {area} index {index} failed: {e}")
                break

            model = _extract_model(resp.text)
            props = (model or {}).get("properties", [])
            if not props:
                break
            for p in props:
                mapped = _map_property(p)
                if mapped:
                    listings.append(mapped)
            if len(props) < PAGE_SIZE:
                break
            index += PAGE_SIZE
            time.sleep(random.uniform(3, 6))   # polite gap between pages
        return listings


def _extract_model(html: str) -> dict | None:
    m = MODEL_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _map_property(p: dict) -> RawListing | None:
    """Map one embedded-model property to RawListing.
    Confirm these keys against a live model; they're centralised here."""
    try:
        price = int((p.get("price") or {}).get("amount") or 0)
        if not price:
            return None
        sub_type = (p.get("propertySubType") or "").lower()
        if "bungalow" in sub_type:
            ptype = "bungalow"
        elif "detached" in sub_type:
            ptype = "detached"
        else:
            ptype = sub_type or "house"
        addr = p.get("displayAddress") or ""
        postcode = _postcode_from(addr)
        images = p.get("propertyImages") or {}
        photo_count = len(images.get("images") or []) or images.get("count") or 0
        loc = p.get("location") or {}
        return RawListing(
            address=addr,
            postcode=postcode,
            price=price,
            property_type=ptype,
            beds=int(p.get("bedrooms") or 0),
            source_portal="rightmove",
            source_listing_id=str(p.get("id") or ""),
            source_url="https://www.rightmove.co.uk" + (p.get("propertyUrl") or ""),
            source_agent=((p.get("customer") or {}).get("branchDisplayName") or ""),
            photo_count=photo_count,
            has_floorplan=bool(p.get("hasFloorPlan") or p.get("floorplanCount")),
            thumb_url=(images.get("mainImageSrc") or ""),
            description_raw=(p.get("summary") or ""),
            lat=loc.get("latitude"),
            lng=loc.get("longitude"),
        )
    except Exception as e:
        print(f"      map failed: {e}")
        return None


def _postcode_from(address: str) -> str:
    m = re.search(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?)\b", address.upper())
    return m.group(1) if m else ""


def _mock_listings() -> list[RawListing]:
    return [
        RawListing(
            address="Beech Hill Road, Rowledge", postcode="GU10 4AH", price=649000,
            property_type="bungalow", beds=3, source_portal="rightmove",
            source_url="https://www.rightmove.co.uk/properties/000001",
            source_agent="Smiths Estates", photo_count=4, has_floorplan=False,
            description_raw=("Executor sale, sold as seen. Detached bungalow set in "
                             "generous grounds with scope to extend. No onward chain."),
            lat=51.196, lng=-0.847,
        ),
        RawListing(
            address="School Lane, Headley", postcode="GU35 8PN", price=795000,
            property_type="smallholding", beds=3, source_portal="rightmove",
            source_url="https://www.rightmove.co.uk/properties/000002",
            source_agent="Rural Property Co", photo_count=12, has_floorplan=True,
            description_raw=("Estate of the late owner. Smallholding of approx 1.2 acres "
                             "with barn benefitting from planning permission for conversion. "
                             "In need of modernisation. Cash buyers preferred."),
            lat=51.118, lng=-0.835,
        ),
    ]
