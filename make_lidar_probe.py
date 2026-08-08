#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_lidar_probe.py -- PROBE (v3) for EA LIDAR download via WCS.

Findings so far:
  v1: catalogue SEARCH works from Actions (HTTP 200) -> IP NOT blocked.
  v2: the /tiles/... download is auth-gated (401 every way), BUT the WCS endpoint's
      GetCapabilities returned HTTP 200 -> WCS is the way in.

v3 nails the WCS GetCoverage call: read the coverage id + axis labels (GetCapabilities +
DescribeCoverage), then request a TINY box around home and confirm a real GeoTIFF comes
back (magic bytes II*/MM*). Tries a few axis/CRS spellings since WCS is fussy. Read-only;
saves nothing but the tiny test tiff is discarded.

  pip install requests pyproj
  python3 make_lidar_probe.py
"""

import re
import sys

_HOME_LAT, _HOME_LON = 51.198, -0.832
WCS = ("https://environment.data.gov.uk/spatialdata/"
       "lidar-composite-digital-terrain-model-dtm-1m/wcs")
UA = ("Mozilla/5.0 (compatible; PropertyScout LIDAR probe; "
      "+personal property research)")
_TIFF = (b"II*\x00", b"MM\x00*")


def _get(s, params, label):
    """GET the WCS with params; report + return (ok_is_tiff, content)."""
    try:
        r = s.get(WCS, params={"service": "WCS", "version": "2.0.1", **params}, timeout=120)
        head = r.content[:4]
        is_tiff = r.status_code == 200 and head in _TIFF
        print("  [%s] HTTP %s | ct=%s | %s bytes | first=%r%s"
              % (label, r.status_code, r.headers.get("content-type"),
                 len(r.content), head, "  <-- GeoTIFF ✓" if is_tiff else ""))
        if r.status_code != 200 and r.content[:400]:
            print("      body:", r.text[:200])
        return is_tiff, r
    except Exception as e:
        print("  [%s] failed: %s" % (label, e))
        return False, None


def main():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA})

    print("=== GetCapabilities -> coverage ids ===")
    ok, cap = _get(s, {"request": "GetCapabilities"}, "GetCapabilities")
    if not cap or cap.status_code != 200:
        print("VERDICT: WCS GetCapabilities not reachable; cannot probe."); return
    ids = re.findall(r"<(?:wcs:)?CoverageId>([^<]+)</(?:wcs:)?CoverageId>", cap.text)
    if not ids:
        ids = re.findall(r"coverageId[\"'>=\s]+([A-Za-z0-9_.:\-]+)", cap.text)
    print("  coverage ids found:", ids[:8] or "(none parsed)")
    cid = ids[0] if ids else None
    if not cid:
        print("  capabilities sample:", cap.text[:400])
        print("VERDICT: could not parse a CoverageId from GetCapabilities."); return

    print("\n=== DescribeCoverage (reveals axis labels + CRS) ===")
    ok, desc = _get(s, {"request": "DescribeCoverage", "coverageId": cid}, "DescribeCoverage")
    if desc is not None and desc.status_code == 200:
        txt = desc.text
        axes = re.findall(r'axisLabels="([^"]+)"', txt)
        lc = re.findall(r"<(?:gml:)?lowerCorner>([^<]+)</(?:gml:)?lowerCorner>", txt)
        uc = re.findall(r"<(?:gml:)?upperCorner>([^<]+)</(?:gml:)?upperCorner>", txt)
        print("  axisLabels:", axes[:3], "| lowerCorner:", lc[:1], "| upperCorner:", uc[:1])
        print("  describe sample:", txt[:300])

    # tiny box around home, in BNG (native) via pyproj
    try:
        from pyproj import Transformer
        e, n = Transformer.from_crs(4326, 27700, always_xy=True).transform(_HOME_LON, _HOME_LAT)
        e, n = int(e), int(n)
        print("\nhome BNG ~ E%d N%d" % (e, n))
    except Exception as ex:
        print("\npyproj unavailable (%s); using a hardcoded in-area BNG box" % ex)
        e, n = 482500, 144800
    de = 200

    print("\n=== GetCoverage attempts (200 m box) ===")
    attempts = [
        {"request": "GetCoverage", "coverageId": cid, "format": "image/tiff",
         "subset": ["E(%d,%d)" % (e - de, e + de), "N(%d,%d)" % (n - de, n + de)]},
        {"request": "GetCoverage", "coverageId": cid, "format": "image/tiff",
         "subset": ["x(%d,%d)" % (e - de, e + de), "y(%d,%d)" % (n - de, n + de)]},
        {"request": "GetCoverage", "coverageId": cid, "format": "image/geotiff",
         "subset": ["E(%d,%d)" % (e - de, e + de), "N(%d,%d)" % (n - de, n + de)]},
        {"request": "GetCoverage", "coverageId": cid, "format": "image/tiff",
         "subsettingCrs": "http://www.opengis.net/def/crs/EPSG/0/4326",
         "subset": ["Lat(%f,%f)" % (_HOME_LAT - 0.002, _HOME_LAT + 0.002),
                    "Long(%f,%f)" % (_HOME_LON - 0.002, _HOME_LON + 0.002)]},
    ]
    win = None
    for i, p in enumerate(attempts):
        got, _ = _get(s, p, "GetCoverage v%d" % (i + 1))
        if got:
            win = (i + 1, p)
            break

    print("\n=== VERDICT ===")
    if win:
        print("WCS GetCoverage WORKS ✓ (attempt %d) coverageId=%s" % (win[0], cid))
        print("  -> build the pipeline: WCS GetCoverage the bowl bbox for DTM + first-return")
        print("     DSM, nDSM = DSM - DTM, per-building ridge/mean height (rasterio +")
        print("     rasterstats), emit building_heights.json.gz. Never commit raster.")
    else:
        print("GetCapabilities/DescribeCoverage worked but no GetCoverage variant returned a")
        print("GeoTIFF. Read the DescribeCoverage axisLabels above -- the subset axis names")
        print("or CRS need to match those exactly; that's the last detail to fix.")


if __name__ == "__main__":
    main()
