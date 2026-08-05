#!/usr/bin/env python3
"""
make_uprn.py - build uprn_coords.json.gz from the free OS Open UPRN bulk download.

Run this LOCALLY (not on GitHub Actions), then upload the resulting
uprn_coords.json.gz to the repo root via the GitHub web UI - exactly the same
"download once, query locally" pattern as the INSPIRE parcels, OS footprints and
Price Paid files (the sources that have never failed).

WHY THIS MATTERS
    run.py geocodes properties by POSTCODE CENTROID. A centroid frequently lands
    inside the wrong, larger ENCLOSING parcel, so the plot/footprint it reports is
    a whole estate or field, not the house's own plot - the single biggest accuracy
    bug in the tool. When run.py can resolve a property's UPRN (from its EPC
    certificate, or a portal feed) to a precise coordinate, it snaps the property
    onto that point and parcel_for matches the correct plot. This file is that
    UPRN -> coordinate lookup. It is OPTIONAL: without it, run.py behaves exactly
    as before (centroid geocoding, plots flagged "approx-location").

INPUT - OS Open UPRN, free under the Open Government Licence:
    https://osdatahub.os.uk/downloads/open/OpenUPRN   (choose the CSV format)
    The CSV header is:  UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE
    (LATITUDE/LONGITUDE are WGS84/ETRS89 degrees, so no coordinate maths needed.)

USAGE
    python make_uprn.py osopenuprn_202xxx.csv                # -> uprn_coords.json.gz
    python make_uprn.py osopenuprn.csv  uprn_coords.json.gz  # explicit output path

Only rows inside BBOX (the Farnham/Wrecclesham search area + a margin) are kept, so
the output stays a few MB gzipped and is uploadable from the phone web UI. Widen
BBOX if you extend AREA_POLYGON / the probate / auction radius in run.py.
"""
import csv
import gzip
import json
import sys

# (min_lat, max_lat, min_lng, max_lng) - covers AREA_POLYGON in run.py plus a generous
# margin for the probate (8 mi) and auction (20 mi) search radii around home.
BBOX = (51.05, 51.40, -1.00, -0.55)


def main():
    if len(sys.argv) < 2:
        print("usage: python make_uprn.py <osopenuprn.csv> [out.json.gz]")
        sys.exit(1)
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "uprn_coords.json.gz"
    lo_lat, hi_lat, lo_lng, hi_lng = BBOX

    uprns = {}
    seen = 0
    with open(src, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # tolerate column-name casing / whitespace differences between OS releases
        cols = {(c or "").lower().strip(): c for c in (reader.fieldnames or [])}
        u_c, la_c, lo_c = cols.get("uprn"), cols.get("latitude"), cols.get("longitude")
        if not (u_c and la_c and lo_c):
            print(f"error: expected UPRN/LATITUDE/LONGITUDE columns, got {reader.fieldnames}")
            print("If your CSV only has X_COORDINATE/Y_COORDINATE (British National Grid),")
            print("re-download the standard OS Open UPRN CSV which includes lat/long.")
            sys.exit(2)
        for row in reader:
            seen += 1
            try:
                lat = float(row[la_c])
                lng = float(row[lo_c])
            except (TypeError, ValueError):
                continue
            if lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng:
                uprn = str(row[u_c]).strip()
                if uprn:
                    uprns[uprn] = [round(lat, 6), round(lng, 6)]  # ~0.1 m precision

    with gzip.open(out, "wt") as f:
        json.dump({"uprns": uprns, "bbox": list(BBOX)}, f)
    print(f"read {seen:,} rows, kept {len(uprns):,} inside bbox -> {out}")
    if not uprns:
        print("warning: 0 UPRNs kept - check the CSV covers your area and BBOX is right.")


if __name__ == "__main__":
    main()
