#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_lidar_probe.py -- PROBE (v2) for EA LIDAR Composite 1m download.

v1 proved the catalogue SEARCH works from Actions (HTTP 200, real tiles) -- so GitHub's
IP is NOT blocked. Two things were wrong: the product is labelled "DTM"/"DSM" (not
"terrain"/"surface"), and the tile DOWNLOAD via {uri}?subscription-key=public returned 401.

v2 finds the correct download call: it dumps one full result record (to reveal the real
download link/fields), then tries several download methods on ONE DTM-1m tile, plus the
WCS route -- reporting the status of each. Downloads at most a few bytes; commits nothing.

  pip install requests
  python3 make_lidar_probe.py
"""

import json
import sys

_HOME_LAT, _HOME_LON = 51.198, -0.832
_D = 0.03
AOI = {"type": "Polygon", "coordinates": [[
    [_HOME_LON - _D, _HOME_LAT - _D], [_HOME_LON + _D, _HOME_LAT - _D],
    [_HOME_LON + _D, _HOME_LAT + _D], [_HOME_LON - _D, _HOME_LAT + _D],
    [_HOME_LON - _D, _HOME_LAT - _D]]]}
SEARCH = "https://environment.data.gov.uk/backend/catalog/api/tiles/collections/survey/search"
WCS_DTM = ("https://environment.data.gov.uk/spatialdata/"
           "lidar-composite-digital-terrain-model-dtm-1m/wcs")
UA = ("Mozilla/5.0 (compatible; PropertyScout LIDAR probe; +personal property research; "
      "contact: heystevenridgeway@gmail.com)")


def _first_bytes(session, url, headers=None, label=""):
    try:
        with session.get(url, headers=headers, timeout=90, stream=True) as g:
            head = next(g.iter_content(chunk_size=16), b"")
            zip_ok = g.status_code == 200 and head[:2] == b"PK"
            print("  [%s] HTTP %s | ct=%s | len=%s | first=%r %s"
                  % (label, g.status_code, g.headers.get("content-type"),
                     g.headers.get("content-length"), head[:12],
                     "  <-- ZIP OK" if zip_ok else ""))
            return zip_ok
    except Exception as e:
        print("  [%s] failed: %s" % (label, e))
        return False


def main():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA})

    print("=== 1) catalogue search ===")
    try:
        r = s.post(SEARCH, json=AOI, timeout=60, headers={
            "Content-Type": "application/geo+json", "Accept": "application/json",
            "Origin": "https://environment.data.gov.uk",
            "Referer": "https://environment.data.gov.uk/",
            "Cookie": "defra-cookie-banner-dismissed=true"})
        print("  HTTP", r.status_code, "| count:", (r.json().get("count") if r.ok else "-"))
        results = r.json().get("results", []) if r.ok else []
    except Exception as e:
        print("  search failed:", e); return
    if not results:
        print("VERDICT: no results; cannot probe download."); return

    # dump ONE full record so we can see EVERY field (esp. any real download link)
    print("\n=== 2) full first record (all fields) ===")
    print(json.dumps(results[0], indent=1)[:1400])

    # DTM 1m tiles, matched on product.id + resolution.id (the correct fields)
    def is_dtm1(it):
        return (((it.get("product") or {}).get("id") == "lidar_composite_dtm")
                and str((it.get("resolution") or {}).get("id")) == "1")
    dtm1 = [it for it in results if is_dtm1(it)]
    print("\nDTM-1m tiles matched:", len(dtm1))
    if not dtm1:
        print("VERDICT: matcher still wrong -- inspect the record above."); return
    it = dtm1[0]
    uri = it.get("uri") or ""
    tile = (it.get("tile") or {}).get("id")
    print("picked tile", tile, "uri:", uri)

    # collect any link-like fields from the record to try as downloads
    cand_urls = []
    if uri:
        cand_urls.append(("uri", uri))
    for k in ("download", "url", "asset", "href"):
        if it.get(k):
            cand_urls.append((k, it[k]))
    for ln in (it.get("links") or []):
        if isinstance(ln, dict) and ln.get("href"):
            cand_urls.append(("link:%s" % ln.get("rel"), ln["href"]))

    print("\n=== 3) download attempts on ONE DTM-1m tile ===")
    ok = False
    hdr_key = {"Ocp-Apim-Subscription-Key": "public"}
    for name, u in cand_urls:
        ok = _first_bytes(s, u, label="%s (bare)" % name) or ok
        ok = _first_bytes(s, u + ("&" if "?" in u else "?") + "subscription-key=public",
                          label="%s ?key" % name) or ok
        ok = _first_bytes(s, u, headers=hdr_key, label="%s hdr-key" % name) or ok
        ok = _first_bytes(s, u + ("&" if "?" in u else "?") + "f=zip", label="%s ?f=zip" % name) or ok
        if ok:
            break

    print("\n=== 4) WCS route (bbox -> GeoTIFF, no tiles) ===")
    try:
        cap = s.get(WCS_DTM, params={"service": "WCS", "version": "2.0.1",
                                     "request": "GetCapabilities"}, timeout=60)
        print("  GetCapabilities HTTP %s | ct=%s | starts %r"
              % (cap.status_code, cap.headers.get("content-type"), cap.text[:80]))
    except Exception as e:
        print("  WCS GetCapabilities failed:", e)

    print("\n=== VERDICT ===")
    if ok:
        print("Tile DOWNLOAD works ✓ -- note which method above succeeded; build the pipeline on it.")
    else:
        print("Search works but no download method returned a zip. Read the full record (part 2)")
        print("and the attempt statuses (part 3) -- the real download field/auth is in there,")
        print("or the WCS route (part 4) is the way in.")


if __name__ == "__main__":
    main()
