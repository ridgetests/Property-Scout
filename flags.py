"""
Search-invisibility flags. Each flag is a reason a property is hiding from
the crowd — the things that make it mispriced because fewer buyers find or
engage with it. Surfaced as chips in the UI.
"""
from __future__ import annotations
import re


def detect(prop) -> list[str]:
    flags = []
    desc = (prop.description_raw or "").lower()
    media = prop.media or {}
    plot = prop.enrichment.get("plot") or {}

    if (media.get("photo_count") or 0) < 6:
        flags.append("few_photos")
    if not media.get("has_floorplan"):
        flags.append("no_floorplan")

    acres = plot.get("area_acres")
    if acres and acres >= 0.25 and not re.search(r"\b(acre|plot|grounds|paddock)\b", desc):
        flags.append("plot_not_tagged")

    for threshold in (500_000, 600_000, 650_000, 700_000, 750_000):
        if threshold < prop.price <= threshold * 1.02:
            flags.append("priced_just_above")
            break

    if "cash buyer" in desc or "cash only" in desc:
        flags.append("cash_buyers_only")
    if "sold as seen" in desc:
        flags.append("sold_as_seen")

    # Bedroom mismatch heuristic: a study/office mentioned alongside the bed count
    if re.search(r"\bstudy\b|\boffice\b|\bsnug\b", desc) and prop.beds <= 4:
        flags.append("possible_extra_bed")

    return flags
