"""
Canonical data shapes shared across the whole pipeline.

RawListing is what every SOURCE adapter must return — a thin, common shape
regardless of whether it came from a scraper or a paid API.

Property is the enriched, scored canonical record that gets stored and
published to properties.json.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import hashlib
import re


def make_id(address: str, postcode: str) -> str:
    """Stable key from address + postcode, resistant to minor formatting drift."""
    norm = re.sub(r"[^a-z0-9]", "", (address + postcode).lower())
    return hashlib.sha1(norm.encode()).hexdigest()[:8]


@dataclass
class RawListing:
    """The common shape every source adapter returns."""
    address: str
    postcode: str
    price: int
    property_type: str
    beds: int
    source_portal: str            # "rightmove" | "auction" | "off-market" ...
    source_url: str = ""
    source_listing_id: str = ""
    source_agent: str = ""
    photo_count: int = 0
    has_floorplan: bool = False
    thumb_url: str = ""
    description_raw: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None

    @property
    def id(self) -> str:
        return make_id(self.address, self.postcode)


@dataclass
class Property:
    """Enriched, scored, canonical record."""
    id: str
    address: str
    postcode: str
    price: int
    property_type: str
    beds: int
    lat: Optional[float] = None
    lng: Optional[float] = None
    first_seen: str = ""
    last_seen: str = ""
    days_on_market: int = 0
    status: str = "live"
    relisted_count: int = 0
    source: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    description_raw: str = ""
    enrichment: dict = field(default_factory=dict)
    comps: list = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)
    score: int = 0
    low_comp: bool = False
    scored_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
