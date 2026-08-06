#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_building_heights.py -- add LIDAR height to the barn candidates.

The probe proved EA LIDAR downloads via WCS GetCoverage (native BNG, axis E/N, image/tiff)
from GitHub Actions. Height is what finally separates a LOW barn from a TALL house: for
each ELIGIBLE candidate we fetch its ground surface (DTM) and its top surface (first-return
DSM) for a small box, subtract to get height-above-ground (nDSM), and record the ridge
(max) and mean height. A "barn is big but low"; a house is "tall for its footprint".

Reads:  docs/barn_candidates.json   (the designation-gated shortlist)
Writes: docs/building_heights.json  (keyed "lat,lon" -> {ridge_m, mean_m, storeys, kind})

Only ELIGIBLE candidates are sampled (the actionable set) -> ~2 requests each, spaced,
behind a simple breaker. Heavy/occasional: the "Build building heights" workflow button,
NOT the nightly cron. Never commits raster.

  pip install requests pyproj rasterio numpy
  python3 make_building_heights.py
"""

import json
import os
import re
import sys
import time

DTM_WCS = ("https://environment.data.gov.uk/spatialdata/"
           "lidar-composite-digital-terrain-model-dtm-1m/wcs")
DSM_WCS = ("https://environment.data.gov.uk/spatialdata/"
           "lidar-composite-digital-surface-model-first-return-dsm-1m/wcs")
CANDS = os.environ.get("PS_CANDS", "docs/barn_candidates.json")
OUTPUT = os.environ.get("PS_OUTPUT", "docs/building_heights.json")
DELAY = float(os.environ.get("PS_WCS_DELAY", "0.4"))
UA = ("Mozilla/5.0 (compatible; PropertyScout LIDAR heights; +personal property research; "
      "contact: heystevenridgeway@gmail.com)")


def _coverage_id(session, wcs, kind):
    """The 'Elevation' coverage id for a WCS endpoint (not the Hillshade one)."""
    r = session.get(wcs, params={"service": "WCS", "version": "2.0.1",
                                 "request": "GetCapabilities"}, timeout=60)
    r.raise_for_status()
    ids = re.findall(r"<(?:wcs:)?CoverageId>([^<]+)</(?:wcs:)?CoverageId>", r.text)
    for cid in ids:
        if "elevation" in cid.lower() and kind.lower() in cid.lower() and "hillshade" not in cid.lower():
            return cid
    return ids[0] if ids else None


def _coverage(session, wcs, cid, e0, e1, n0, n1):
    """WCS GetCoverage a small BNG box -> (array float32 nodata->nan, transform). None on miss."""
    import numpy as np
    from rasterio.io import MemoryFile
    r = session.get(wcs, params={
        "service": "WCS", "version": "2.0.1", "request": "GetCoverage",
        "coverageId": cid, "format": "image/tiff",
        "subset": ["E(%d,%d)" % (e0, e1), "N(%d,%d)" % (n0, n1)]}, timeout=90)
    if r.status_code != 200 or r.content[:4] not in (b"II*\x00", b"MM\x00*"):
        return None
    with MemoryFile(r.content) as mf, mf.open() as ds:
        arr = ds.read(1).astype("float32")
        if ds.nodata is not None:
            arr[arr == ds.nodata] = np.nan
        arr[arr < -100] = np.nan          # LIDAR sentinel nodata
        return arr, ds.transform


def _load_rings():
    """centroid-key -> building outline ring [[lat,lon],...] from building_polygons.json.gz,
    so we sample the BUILDING's roof, not a box that catches nearby trees."""
    import gzip
    rings = {}
    try:
        with gzip.open("building_polygons.json.gz", "rt") as f:
            for b in json.load(f).get("buildings", []):
                c = b.get("c")
                if c and b.get("r"):
                    rings["%s,%s" % (c[0], c[1])] = b["r"]
    except Exception as e:
        print("  (building_polygons.json.gz unavailable: %s; falling back to boxes)" % e)
    return rings


def main():
    import numpy as np
    from pyproj import Transformer

    try:
        cands = json.load(open(CANDS)).get("candidates", [])
    except Exception as e:
        sys.exit("cannot read %s (%s)" % (CANDS, e))
    elig = [c for c in cands if (c.get("designation") or {}).get("eligible")]
    todo = elig or cands
    print("candidates: %d | eligible (sampled): %d" % (len(cands), len(todo)))

    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    dtm_id = _coverage_id(s, DTM_WCS, "DTM")
    dsm_id = _coverage_id(s, DSM_WCS, "DSM")
    print("DTM coverage:", dtm_id)
    print("DSM coverage:", dsm_id)
    if not dtm_id or not dsm_id:
        sys.exit("ABORT: could not resolve WCS coverage ids.")

    from rasterio.features import rasterize
    rings = _load_rings()
    print("building outlines available:", len(rings))
    to_bng = Transformer.from_crs(4326, 27700, always_xy=True)
    heights, dead, sampled, boxed = {}, 0, 0, 0
    for i, c in enumerate(todo):
        e, n = to_bng.transform(c["lon"], c["lat"])
        e, n = int(e), int(n)
        ring = rings.get("%s,%s" % (c["lat"], c["lon"]))
        # BNG polygon of the building outline; else a tight box (last resort)
        if ring:
            pts = [to_bng.transform(lon, lat) for lat, lon in ring]  # ring is [lat,lon]
            geom = {"type": "Polygon", "coordinates": [[[px, py] for px, py in pts]]}
            es = [p[0] for p in pts]; ns = [p[1] for p in pts]
            e0, e1, n0, n1 = int(min(es)) - 2, int(max(es)) + 2, int(min(ns)) - 2, int(max(ns)) + 2
        else:
            boxed += 1
            hw = int(max(5, (c.get("area_m2", 200) ** 0.5) / 2 * 0.5))  # inner core only
            geom, e0, e1, n0, n1 = None, e - hw, e + hw, n - hw, n + hw
        try:
            dtm = _coverage(s, DTM_WCS, dtm_id, e0, e1, n0, n1)
            time.sleep(DELAY)
            dsm = _coverage(s, DSM_WCS, dsm_id, e0, e1, n0, n1)
            time.sleep(DELAY)
        except Exception as ex:
            print("  %d: WCS error %s" % (i, ex))
            dead += 1
            if dead >= 5:
                print("  too many WCS errors -- stopping early to protect access")
                break
            continue
        if dtm is None or dsm is None:
            continue
        (dtm_a, tr), (dsm_a, _) = dtm, dsm
        h, w = min(dtm_a.shape[0], dsm_a.shape[0]), min(dtm_a.shape[1], dsm_a.shape[1])
        ndsm = dsm_a[:h, :w] - dtm_a[:h, :w]
        if geom is not None:
            # mask to the building footprint so nearby trees/ground are excluded
            mask = rasterize([(geom, 1)], out_shape=(h, w), transform=tr,
                             fill=0, dtype="uint8").astype(bool)
            vals = ndsm[mask & np.isfinite(ndsm)]
        else:
            vals = ndsm[np.isfinite(ndsm)]
        if vals.size < 4:
            continue
        # 95th percentile = ridge (robust to a stray tall pixel); avoids single-tree spikes
        ridge = float(np.percentile(vals, 95))
        mean = float(np.mean(vals[vals > 0.5])) if np.any(vals > 0.5) else 0.0
        # a barn is LOW for its size; a house is tall. Rough single-signal read:
        storeys = 1 if ridge < 6.5 else (2 if ridge < 10.0 else 3)
        kind = ("low (barn-like)" if ridge < 7.0 else
                "mid" if ridge < 9.5 else "tall (house-like)")
        heights["%s,%s" % (c["lat"], c["lon"])] = {
            "ridge_m": round(ridge, 1), "mean_m": round(mean, 1),
            "storeys": storeys, "kind": kind}
        sampled += 1
        if sampled % 20 == 0:
            print("  sampled %d/%d..." % (sampled, len(todo)))

    from collections import Counter
    kinds = Counter(v["kind"] for v in heights.values())
    print("\nHEIGHTS: sampled %d of %d (%d fell back to a box, no outline)"
          % (sampled, len(todo), boxed))
    for k in ("low (barn-like)", "mid", "tall (house-like)"):
        print("  %-18s : %d" % (k, kinds.get(k, 0)))

    if not heights:
        sys.exit("ABORT: no heights sampled (WCS coverage or params off). Writing nothing.")

    out = {"heights": heights, "sampled": sampled,
           "note": "LIDAR height above ground (first-return DSM minus DTM) per eligible "
                   "barn candidate. 'tall (house-like)' >= 9.5 m to ridge is probably a "
                   "house, not a barn. Indicative; verify on imagery."}
    with open(OUTPUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s (%d, %.0f KB)" % (OUTPUT, sampled, os.path.getsize(OUTPUT) / 1024.0))


if __name__ == "__main__":
    main()
