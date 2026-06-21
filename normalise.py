"""
Maps a RawListing (whatever its source) onto the canonical Property record.
This is the only place that knows how to translate the common raw shape into
the stored shape, so adapters stay dumb and uniform.
"""
from __future__ import annotations
from pipeline.models import RawListing, Property


def normalise(raw: RawListing) -> Property:
    return Property(
        id=raw.id,
        address=raw.address,
        postcode=raw.postcode,
        price=raw.price,
        property_type=raw.property_type,
        beds=raw.beds,
        lat=raw.lat,
        lng=raw.lng,
        source={
            "portal": raw.source_portal,
            "listing_id": raw.source_listing_id,
            "url": raw.source_url,
            "agent": raw.source_agent,
        },
        media={
            "photo_count": raw.photo_count,
            "has_floorplan": raw.has_floorplan,
            "thumb_url": raw.thumb_url,
        },
        description_raw=raw.description_raw,
    )
