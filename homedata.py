"""
Homedata source adapter — listing discovery via a licensed API.

Why this over scraping:
  * It's an authenticated API (key in a header), so no IP blocking and it runs
    fine from GitHub Actions — unlike scraping, which gets blocked from cloud IPs.
  * It aggregates Rightmove, Zoopla and OnTheMarket, de-duplicated and
    UPRN-matched, and exposes the full listing-event chain (added, reduced,
    sold STC, withdrawn, re-listed) plus days-on-market and reduction counts.

Auth (confirmed): every request carries  Authorization: Api-Key YOUR_KEY
Set HOMEDATA_API_KEY as an environment variable / Action secret.

-- TWO THINGS TO CONFIRM IN THEIR PLAYGROUND (homedata.co.uk/docs) ----------
  1. The exact listings path + query-param names. Homedata is mid-migration
     across hosts (homedata.co.uk/api/v1, neo.homedata.co.uk, api.homedata.co.uk).
     The auth header and the field names below are stable; only the path/params
     need a quick check. They're centralised in LISTINGS_PATH / _params.
  2. Plan: area-wide listing search (bulk export) is a paid-plan feature, not
     the free tier. The free tier is for testing + per-property enrichment.
----------------------------------------------------------------------------
"""
from __future__ import annotations
import os
import time
import requests
from pipeline.models import RawListing
from adapters.base import SourceAdapter

USE_MOCK = False   # flip to False once you have a key and confirmed the path

API_KEY = os.environ.get("HOMEDATA_API_KEY", "")
BASE = os.environ.get("HOMEDATA_BASE", "https://homedata.co.uk/api/v1")
LISTINGS_PATH = "/listings"          # confirm exact path in the playground
HEADERS = {"Authorization": f"Api-Key {API_KEY}", "Accept": "application/json"}


class HomedataSourceAdapter(SourceAdapter):
    name = "homedata"
    cost = "paid"

    def fetch(self, criteria: dict) -> list[RawListing]:
        if USE_MOCK or not API_KEY:
            return _mock_listings()
        out = []
        for area in criteria.get("areas", []):
            out.extend(self._fetch_area(area, criteria))
            time.sleep(0.4)
        return out

    def _fetch_area(self, area: str, criteria: dict) -> list[RawListing]:
        try:
            r = requests.get(BASE + LISTINGS_PATH, params=_params(area, criteria),
                             headers=HEADERS, timeout=25)
            if r.status_code == 429:
                print("      Homedata rate limit hit — backing off")
                time.sleep(5)
                r = requests.get(BASE + LISTINGS_PATH, params=_params(area, criteria),
                                 headers=HEADERS, timeout=25)
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            print(f"      Homedata listings failed for {area}: {e}")
            return []
        rows = body.get("listings") or body.get("data") or body.get("results") or []
        return [m for m in (_map(row) for row in rows) if m]


def _params(area: str, criteria: dict) -> dict:
    # Confirm param names in the playground; centralised here so it's a 1-place fix.
    return {
        "outcode": area,
        "min_price": criteria.get("min_price"),
        "max_price": criteria.get("max_price"),
        "min_bedrooms": criteria.get("min_beds"),
        "property_type": "detached,bungalow",
        "status": "live",
        "rows": 100,
    }


def _ptype(raw_type: str) -> str:
    t = (raw_type or "").lower()
    if "bungalow" in t:
        return "bungalow"
    if "detached" in t:
        return "detached"
    return t or "house"


def _map(row: dict) -> RawListing | None:
    """Map a Homedata listing row to RawListing. Field names per their
    bulk-export schema: listing_id, address, postcode, uprn, price,
    original_price, bedrooms, property_type, status, dom, listed_date,
    agent, reductions, lat, lng, construction_age."""
    try:
        price = int(row.get("price") or 0)
        if not price:
            return None
        return RawListing(
            address=row.get("address") or "",
            postcode=row.get("postcode") or "",
            price=price,
            property_type=_ptype(row.get("property_type")),
            beds=int(row.get("bedrooms") or 0),
            source_portal="homedata",
            source_listing_id=str(row.get("listing_id") or ""),
            source_url=row.get("url") or "",
            source_agent=row.get("agent") or "",
            uprn=str(row.get("uprn") or ""),
            lat=row.get("lat"),
            lng=row.get("lng"),
            # Homedata gives no free-text description; market signals carry the
            # motivation info instead (reductions, days-on-market, status chain).
            description_raw="",
            market={
                "original_price": row.get("original_price"),
                "reductions": row.get("reductions"),
                "status": row.get("status"),
                "dom": row.get("dom"),
                "listed_date": row.get("listed_date"),
                "construction_age": row.get("construction_age"),
            },
        )
    except Exception as e:
        print(f"      Homedata map failed: {e}")
        return None


def _mock_listings() -> list[RawListing]:
    return [
        RawListing(
            address="Beech Hill Road, Rowledge", postcode="GU10 4AH", price=649000,
            property_type="bungalow", beds=3, source_portal="homedata",
            source_listing_id="hd_0001", uprn="100061234567",
            source_agent="Smiths Estates", lat=51.196, lng=-0.847,
            market={"original_price": 675000, "reductions": 2, "status": "reduced",
                    "dom": 61, "listed_date": "2026-04-02", "construction_age": "1950-1966"},
        ),
        RawListing(
            address="School Lane, Headley", postcode="GU35 8PN", price=795000,
            property_type="detached", beds=3, source_portal="homedata",
            source_listing_id="hd_0002", uprn="100061234890",
            source_agent="Rural Property Co", lat=51.118, lng=-0.835,
            market={"original_price": 850000, "reductions": 3, "status": "reduced",
                    "dom": 104, "listed_date": "2026-03-08", "construction_age": "before 1900"},
        ),
    ]
