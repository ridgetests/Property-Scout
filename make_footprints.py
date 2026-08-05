#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_footprints.py -- build building-OUTLINE polygons for the barn/Class-Q engine.

WHY THIS EXISTS
---------------
The existing footprints_bowl.json.gz holds only {area, centre-point} -- no shape.
The barn scanner needs OUTLINES to measure shape (a barn is a big simple
rectangle) and elongation (sheds are long). This converter produces those
outlines, matching the parcels schema so one set of helpers serves both:

    {"a": area_m2, "b": [latmin, latmax, lonmin, lonmax], "r": [[lat, lon], ...],
     "c": [lat, lon],            # centroid, kept for backward-compat with run.py
     "u": "barn"}                # OSM building/use tag when informative

SOURCE: OpenStreetMap, via Geofabrik county extracts (.osm.pbf).
  - OSM is the ONLY free source that carries building-USE tags
    (building=barn / farm_auxiliary / stable, landuse=farmyard) -- a building
    already tagged "barn" is a near-certain barn, skipping the shape/height
    guess entirely. Licence: ODbL (share-alike -- surface derived facts only).
  - Farnham straddles Surrey/Hampshire, so we read BOTH county extracts and
    dedupe. (GU9 is Surrey; GU10 crosses into Hampshire.)

DELIBERATELY a SEPARATE file (building_polygons.json.gz), NOT a replacement for
footprints_bowl.json.gz: the core tool sizes listings from the OS footprints, and
silently swapping OS->OSM could regress that. Decouple the barn engine from the
live tool; unify later only if OSM proves as complete.

RUN (as a GitHub Actions workflow button -- the user is phone-only):
  pip install pyrosm
  # download surrey-latest.osm.pbf + hampshire-latest.osm.pbf from Geofabrik
  python3 make_footprints.py surrey-latest.osm.pbf hampshire-latest.osm.pbf

Emits building_polygons.json.gz (committed to the repo root), and prints a loud
data-quality report. FAILS (exit 1, writes nothing) if the result looks wrong --
no data beats bad data.
"""

import gzip
import json
import math
import os
import sys

# ------------------------------------------------------------------ config
OUTPUT = os.environ.get("PS_OUTPUT", "building_polygons.json.gz")

# A building's OSM tag value that marks it as agricultural / likely-convertible.
# (Captured verbatim into "u"; the scanner treats these as a strong prior.)
AGRI_TAGS = {"barn", "farm_auxiliary", "cowshed", "stable", "stables",
             "sty", "greenhouse", "farm", "agricultural", "shed", "hangar",
             "warehouse", "storage_tank", "silo"}

# Data-quality thresholds (mirrors the barn brief). Fail loudly rather than
# commit a broken file.
AREA_SANITY_CEILING = 20000.0     # m2; a single building above this is suspect
MAX_ZERO_AREA_FRAC = 0.01         # >1% zero-area => geometry handling is wrong
MIN_BUILDINGS = 500               # far below any real count for this area


def _area_bbox_for_clip():
    """The catchment to clip to. Prefer run.py's AREA_POLYGON (kept consistent
    with the live tool); fall back to a generous hand-set bbox if run.py can't be
    imported. Returns (bbox=[minlon,minlat,maxlon,maxlat], in_poly_fn or None)."""
    try:
        from run import AREA_POLYGON, _in_polygon
        lats = [p[0] for p in AREA_POLYGON]
        lngs = [p[1] for p in AREA_POLYGON]
        m = 0.02   # ~2km margin so nothing on the edge is clipped early
        bbox = [min(lngs) - m, min(lats) - m, max(lngs) + m, max(lats) + m]
        return bbox, (lambda lat, lon: _in_polygon(lat, lon, AREA_POLYGON))
    except Exception as e:
        print(f"  (could not import AREA_POLYGON from run.py: {e}; using fallback bbox)")
        # Generous bowl bbox (Farnham/Wrecclesham and surrounds).
        return [-0.95, 51.10, -0.60, 51.30], None


def _largest_polygon(geom):
    """Return the largest simple Polygon from a Polygon/MultiPolygon (never
    concatenate rings -- that is exactly what corrupted the old footprints)."""
    gt = geom.geom_type
    if gt == "Polygon":
        return geom
    if gt == "MultiPolygon":
        return max(geom.geoms, key=lambda g: g.area)
    return None


def main():
    pbf_paths = [a for a in sys.argv[1:] if a.endswith(".pbf")]
    if not pbf_paths:
        pbf_paths = ["surrey-latest.osm.pbf", "hampshire-latest.osm.pbf"]
    missing = [p for p in pbf_paths if not os.path.exists(p)]
    if missing:
        sys.exit("MISSING pbf file(s): %s\n  Download from Geofabrik first "
                 "(the workflow does this)." % ", ".join(missing))

    try:
        from pyrosm import OSM
    except Exception as e:
        sys.exit("pyrosm not available (%s). `pip install pyrosm`." % e)

    bbox, in_poly = _area_bbox_for_clip()
    print("clip bbox (lon/lat): %s" % bbox)
    print("precise polygon clip: %s" % ("yes (run.AREA_POLYGON)" if in_poly else "no (bbox only)"))

    seen = set()            # dedupe across the two county extracts
    records = []
    n_multi = n_zero = n_huge = n_agri = 0

    for path in pbf_paths:
        print("\n--- reading %s ---" % path)
        osm = OSM(path, bounding_box=bbox)
        gdf = osm.get_buildings()
        if gdf is None or len(gdf) == 0:
            print("  no buildings returned from %s" % path)
            continue
        print("  %d building rows in bbox" % len(gdf))
        # area in British National Grid (metres), computed once for the whole frame
        try:
            areas = gdf.to_crs(27700).geometry.area
        except Exception as e:
            sys.exit("  reprojection to EPSG:27700 failed (%s). Is pyproj installed?" % e)

        tagcol = gdf["building"] if "building" in gdf.columns else None
        for idx, geom in gdf.geometry.items():
            if geom is None or geom.is_empty:
                continue
            if geom.geom_type == "MultiPolygon":
                n_multi += 1
            poly = _largest_polygon(geom)
            if poly is None:
                continue
            c = poly.centroid
            lat, lon = round(c.y, 6), round(c.x, 6)
            if in_poly and not in_poly(lat, lon):
                continue
            key = (lat, lon)
            if key in seen:
                continue
            seen.add(key)

            a = float(areas.get(idx, 0.0) or 0.0)
            if a <= 0.0:
                n_zero += 1
                continue                      # a real building has real area
            if a > AREA_SANITY_CEILING:
                n_huge += 1
                continue                      # merged/spurious geometry -- drop

            # exterior ring as [lat, lon] (shapely gives (lon, lat) in EPSG:4326)
            xs = list(poly.exterior.coords)
            ring = [[round(y, 6), round(x, 6)] for (x, y) in xs]
            lats = [p[0] for p in ring]
            lngs = [p[1] for p in ring]

            u = ""
            if tagcol is not None:
                tv = tagcol.get(idx)
                if isinstance(tv, str) and tv and tv.lower() not in ("yes", "house",
                        "residential", "detached", "semidetached_house", "terrace",
                        "apartments", "bungalow"):
                    u = tv.lower()
            if u in AGRI_TAGS:
                n_agri += 1

            records.append({
                "a": int(round(a)),
                "b": [min(lats), max(lats), min(lngs), max(lngs)],
                "r": ring,
                "c": [lat, lon],
                "u": u,
            })

    total = len(records)
    print("\nDATA QUALITY")
    print("  buildings kept              : %d" % total)
    print("  MultiPolygons (largest part): %d" % n_multi)
    print("  dropped zero/negative area  : %d" % n_zero)
    print("  dropped > %d m2 (suspect)  : %d" % (AREA_SANITY_CEILING, n_huge))
    print("  agricultural-tagged (u in AGRI): %d" % n_agri)

    # --- fail loudly rather than commit a broken file ---
    if total < MIN_BUILDINGS:
        sys.exit("\nABORT: only %d buildings -- expected thousands. "
                 "Bad bbox, empty extract, or a parse failure. Writing nothing." % total)
    zero_frac = n_zero / (total + n_zero) if (total + n_zero) else 0.0
    if zero_frac > MAX_ZERO_AREA_FRAC:
        sys.exit("\nABORT: %.1f%% zero-area (>%.0f%%) -- geometry handling is wrong. "
                 "Writing nothing." % (100 * zero_frac, 100 * MAX_ZERO_AREA_FRAC))

    out = {
        "buildings": records,
        "source": "OpenStreetMap via Geofabrik (ODbL) -- building outlines + use tags",
        "crs": "wgs84",
        "counties": [os.path.basename(p) for p in pbf_paths],
        "note": ("Outline polygons for the barn/Class-Q scanner. 'u' is the OSM "
                 "building/use tag when informative. Separate from "
                 "footprints_bowl.json.gz (OS, used for listing sizing)."),
    }
    with gzip.open(OUTPUT, "wt") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print("\nwrote %s  (%d buildings, %.1f MB)"
          % (OUTPUT, total, os.path.getsize(OUTPUT) / 1e6))


if __name__ == "__main__":
    main()
