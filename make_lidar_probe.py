#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_lidar_probe.py -- PROBE (not a pipeline) for EA LIDAR Composite 1m.

The one open question before building a height pipeline: can a GitHub Actions runner
actually fetch EA LIDAR, or do government IPs block the data-centre? This does the
minimum to find out, LOUDLY, over a SMALL area (a ~5km box around home):

  1. POST a small AOI polygon to the new DEFRA catalogue search API and list the
     DTM/DSM 1m tiles it returns (metadata only -- tiny).
  2. Download exactly ONE DTM tile via {uri}?subscription-key=public and report the
     HTTP status, content-type, size and first bytes (a .zip starts with 'PK').
  3. Print a clear verdict. Downloads nothing else.

No account, no key beyond the literal 'public'. workflow_dispatch only; never nightly.
If this works, the next step is the real nDSM (DSM-DTM) -> per-building height join.

  pip install requests
  python3 make_lidar_probe.py
"""

import sys

# Small AOI: a ~5-6 km box around home (Wrecclesham 51.198,-0.832). GeoJSON = [lon,lat].
_HOME_LAT, _HOME_LON = 51.198, -0.832
_D = 0.03
AOI = {
    "type": "Polygon",
    "coordinates": [[
        [_HOME_LON - _D, _HOME_LAT - _D], [_HOME_LON + _D, _HOME_LAT - _D],
        [_HOME_LON + _D, _HOME_LAT + _D], [_HOME_LON - _D, _HOME_LAT + _D],
        [_HOME_LON - _D, _HOME_LAT - _D],
    ]],
}
SEARCH = "https://environment.data.gov.uk/backend/catalog/api/tiles/collections/survey/search"
UA = ("Mozilla/5.0 (compatible; PropertyScout LIDAR probe; +personal property research; "
      "contact: heystevenridgeway@gmail.com)")


def _is_1m(label, want):
    l = (label or "").lower()
    return want in l and ("1m" in l or "1 m" in l)


def main():
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Content-Type": "application/geo+json",
        "Accept": "application/json",
        "Origin": "https://environment.data.gov.uk",
        "Referer": "https://environment.data.gov.uk/",
        "Cookie": "defra-cookie-banner-dismissed=true",
    })

    print("=== 1) catalogue search (POST small AOI) ===")
    print("POST", SEARCH)
    try:
        r = s.post(SEARCH, json=AOI, timeout=60)
        print("  HTTP", r.status_code, "| content-type:", r.headers.get("content-type"))
        print("  body starts:", repr(r.text[:200]))
        if r.status_code != 200:
            print("\nVERDICT: search endpoint did NOT return 200 from this runner.")
            print("  -> if 403/blocked: EA IPs refuse Actions; pivot to Google Earth Engine")
            print("     (asset UK/EA/ENGLAND_1M_TERRAIN/2022) or a manual one-off.")
            return
        data = r.json()
    except Exception as e:
        print("  search failed:", e)
        print("\nVERDICT: could not reach/parse the search API from this runner.")
        return

    results = data.get("results") or data.get("features") or (data if isinstance(data, list) else [])
    print("  results:", len(results))
    dtm, dsm, other = [], [], []
    for it in results:
        # tolerate a couple of shapes
        prod = ((it.get("product") or {}).get("label") if isinstance(it.get("product"), dict)
                else it.get("product")) or it.get("collection") or ""
        res = ((it.get("resolution") or {}).get("id") if isinstance(it.get("resolution"), dict)
               else it.get("resolution")) or ""
        label = it.get("label") or it.get("tile") or ""
        uri = it.get("uri") or it.get("url") or ((it.get("properties") or {}).get("uri"))
        tag = f"{prod} {res} {label}".strip()
        if uri and _is_1m(tag, "terrain"):
            dtm.append((tag, uri))
        elif uri and _is_1m(tag, "surface"):
            dsm.append((tag, uri))
        else:
            other.append((tag, uri))
    print(f"  DTM-1m tiles: {len(dtm)} | DSM-1m tiles: {len(dsm)} | other: {len(other)}")
    for tag, uri in (dtm[:2] + dsm[:2]):
        print("   -", tag, "->", (uri or "")[:90])
    if not dtm and not dsm and other:
        print("  (no obvious DTM/DSM-1m match; raw sample of what came back:)")
        for tag, uri in other[:4]:
            print("   ?", repr(tag), "->", (uri or "")[:90])

    pick = (dtm or dsm or other)
    pick = [p for p in pick if p[1]]
    if not pick:
        print("\nVERDICT: search worked but returned no downloadable tile URI. Inspect the "
              "body sample above; the response shape may have changed.")
        return

    print("\n=== 2) download ONE tile ===")
    tag, uri = pick[0]
    url = uri + ("&" if "?" in uri else "?") + "subscription-key=public"
    print("GET", url[:110])
    try:
        # stream: read only the first chunk to confirm it's a real zip, don't pull 50 MB
        with s.get(url, timeout=120, stream=True) as g:
            print("  HTTP", g.status_code, "| content-type:", g.headers.get("content-type"),
                  "| content-length:", g.headers.get("content-length"))
            head = next(g.iter_content(chunk_size=8), b"")
            print("  first bytes:", head[:8])
            ok = g.status_code == 200 and head[:2] == b"PK"
    except Exception as e:
        print("  download failed:", e)
        ok = False

    print("\n=== VERDICT ===")
    if ok:
        print("LIDAR IS REACHABLE from GitHub Actions ✓")
        print("  -> safe to build the full pipeline: fetch DTM+DSM for the bowl, compute")
        print("     nDSM = DSM - DTM, take per-building ridge/mean height, emit a small")
        print("     building_heights.json.gz (never commit raster).")
    else:
        print("Tile download did NOT confirm as a zip from this runner.")
        print("  -> re-read the diagnostics above; if IP-blocked, pivot to GEE or a manual")
        print("     one-off download of the derived heights only.")


if __name__ == "__main__":
    main()
