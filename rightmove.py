"""
Rightmove source adapter.

STATUS: stub. The structure is real; the selectors need finishing against the
live site, which changes its markup periodically. Returns mock data until you
wire the live fetch, so the rest of the pipeline runs end-to-end today.

IMPORTANT — read before enabling live scraping:
  * Rightmove's terms prohibit scraping. This is for personal, low-volume use.
    Scrape politely: a slow rate, a real user-agent, and cache aggressively so
    you make as few requests as possible. Hammering the site gets you blocked
    and is the fast route to needing paid proxies.
  * Treat this adapter as the FRAGILE layer. When it breaks, the adapter
    pattern means you can drop in a paid listing API (PropertyData, etc.) as a
    replacement without touching anything downstream.
"""
from __future__ import annotations
import time
import random
from pipeline.models import RawListing
from adapters.base import SourceAdapter

USE_MOCK = True   # flip to False once the live fetch below is finished


class RightmoveAdapter(SourceAdapter):
    name = "rightmove"
    cost = "free"

    def fetch(self, criteria: dict) -> list[RawListing]:
        if USE_MOCK:
            return _mock_listings()
        return self._fetch_live(criteria)

    # -- live fetch -------------------------------------------------------
    def _fetch_live(self, criteria: dict) -> list[RawListing]:
        """
        Sketch of the real flow. Finish the selectors against current markup.

        import requests
        from bs4 import BeautifulSoup

        headers = {"User-Agent": "Mozilla/5.0 (...) personal-research"}
        listings = []
        for area in criteria["areas"]:
            url = self._build_search_url(area, criteria)
            html = requests.get(url, headers=headers, timeout=20).text
            soup = BeautifulSoup(html, "html.parser")
            for card in soup.select("CARD_SELECTOR"):          # TODO
                listings.append(RawListing(
                    address=card.select_one("ADDR_SEL").text.strip(),  # TODO
                    postcode=_extract_postcode(...),
                    price=_parse_price(...),
                    property_type=_classify_type(...),
                    beds=...,
                    source_portal="rightmove",
                    source_url=...,
                    source_listing_id=...,
                    source_agent=...,
                    photo_count=len(card.select("IMG_SEL")),       # TODO
                    has_floorplan=_has_floorplan(...),
                    description_raw=_fetch_detail_description(...),
                ))
            time.sleep(random.uniform(3, 7))   # be polite between areas
        return listings
        """
        raise NotImplementedError("Finish _fetch_live, then set USE_MOCK=False")

    def _build_search_url(self, area: str, criteria: dict) -> str:
        return ""   # TODO: map criteria to Rightmove's location identifiers


def _mock_listings() -> list[RawListing]:
    """Two realistic mock rows so the pipeline produces output immediately."""
    return [
        RawListing(
            address="Beech Hill Road, Rowledge",
            postcode="GU10 4AH", price=649000,
            property_type="bungalow", beds=3,
            source_portal="rightmove",
            source_url="https://www.rightmove.co.uk/properties/000001",
            source_agent="Smiths Estates",
            photo_count=4, has_floorplan=False,
            description_raw=("Executor sale, sold as seen. Detached bungalow set "
                             "in generous grounds with scope to extend. No onward chain."),
            lat=51.196, lng=-0.847,
        ),
        RawListing(
            address="School Lane, Headley",
            postcode="GU35 8PN", price=795000,
            property_type="smallholding", beds=3,
            source_portal="rightmove",
            source_url="https://www.rightmove.co.uk/properties/000002",
            source_agent="Rural Property Co",
            photo_count=12, has_floorplan=True,
            description_raw=("Estate of the late owner. Smallholding of approx 1.2 acres "
                             "with barn benefitting from planning permission for conversion. "
                             "In need of modernisation. Cash buyers preferred."),
            lat=51.118, lng=-0.835,
        ),
    ]
