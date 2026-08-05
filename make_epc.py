#!/usr/bin/env python3
"""
make_epc.py - build epc_region.json.gz from the free EPC Open Data downloads.

Run LOCALLY (not on GitHub Actions), then upload epc_region.json.gz to the repo
root via the GitHub web UI - same "download once, query locally" pattern as the
INSPIRE / OS / Price Paid / UPRN files.

WHY THIS MATTERS
    It unlocks SIZE-MATCHED valuation. run.py already knows how to value a
    property against comparables of a *similar floor area* priced per m2 (the
    "4 neighbouring 4-bed detached with XX sq ft" method) - find_comps switches
    to it automatically when it can look up each comparable's floor area. That
    lookup is this file. Without it, comps stay type-and-geography matched (still
    useful, just not size-matched). run.py NEVER calls the EPC register for
    comparables - the register is reserved for subject lookups - so this local
    file is the only way comps get floor areas.

INPUT - EPC Open Data (domestic certificates), free under the Open Government
Licence (one free registration for an API key / bulk download):
    https://epc.opendatacommunities.org/  ->  "Download all data"
    Prefer the PER-LOCAL-AUTHORITY downloads (a few MB each) over the multi-GB
    national file. For the Farnham search area you need two:
        Waverley           (LA code E07000216)
        East Hampshire     (LA code E07000085)
    Each download is a ZIP containing certificates.csv.

USAGE - pass the ZIPs, the extracted CSVs, or folders containing certificates.csv:
    python make_epc.py waverley.zip east-hants.zip           # -> epc_region.json.gz
    python make_epc.py certificates.csv                      # single CSV
    python make_epc.py ./epc-downloads/  out.json.gz         # a folder + custom output

OUTPUT - {"certs": {"<POSTCODE>|<PAON>": {"fa","form","type","rooms","date"}}} gz,
keyed to match run.py's comparables (postcode + Price-Paid house number/name,
upper-cased). Most-recent certificate wins when a property has several.
"""
import csv
import gzip
import io
import json
import os
import re
import sys
import zipfile

# Optional outcode filter: keep only these postcode districts (e.g. {"GU9","GU10"}).
# Leave empty to keep everything (per-LA files are already area-scoped; only worth
# setting if you feed a larger national file and want to shrink the output).
OUTCODES = set()

# EPC CSV column names we need (matched case-insensitively).
_WANT = {
    "postcode": "POSTCODE",
    "address1": "ADDRESS1",
    "fa": "TOTAL_FLOOR_AREA",
    "form": "BUILT_FORM",
    "type": "PROPERTY_TYPE",
    "rooms": "NUMBER_HABITABLE_ROOMS",
    "lodged": "LODGEMENT_DATE",
    "inspected": "INSPECTION_DATE",
}


def _norm_pc(pc):
    """Canonical UK postcode to match Price Paid: upper, single space before the
    3-char inward code (e.g. 'gu337ag' / 'GU33  7AG' -> 'GU33 7AG')."""
    s = re.sub(r"\s+", "", (pc or "").upper())
    if len(s) < 5:
        return s
    return s[:-3] + " " + s[-3:]


def _paon(address1):
    """The primary addressable object name, matched to Price Paid's PAON: a leading
    house number (12, 12A) if present, else the building name (text before the first
    comma). Upper-cased at the key so casing never matters."""
    a = (address1 or "").strip()
    m = re.match(r"(\d+[A-Za-z]?)\b", a)
    if m:
        return m.group(1)
    return a.split(",")[0].strip()


def _rows(path):
    """Yield csv rows (as dicts) from a CSV, a ZIP (any *certificates.csv member),
    or a directory tree (any certificates.csv). Tolerates BOM/encoding quirks."""
    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for fn in files:
                if fn.lower().endswith("certificates.csv"):
                    yield from _rows(os.path.join(root, fn))
        return
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith("certificates.csv"):
                    with z.open(name) as fh:
                        text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace")
                        yield from csv.DictReader(text)
        return
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        yield from csv.DictReader(fh)


def main():
    if len(sys.argv) < 2:
        print("usage: python make_epc.py <certificates.csv|LA.zip|folder> [more...] [out.json.gz]")
        sys.exit(1)
    args = sys.argv[1:]
    out = "epc_region.json.gz"
    if args[-1].endswith(".json.gz"):
        out = args.pop()
    if not args:
        print("no input files given")
        sys.exit(1)

    certs = {}
    seen = kept = 0
    colmap = None
    for path in args:
        for row in _rows(path):
            seen += 1
            if colmap is None:
                # resolve actual column names once (case/space-insensitive)
                low = {(k or "").strip().lower(): k for k in row.keys()}
                colmap = {want: low.get(col.lower()) for want, col in _WANT.items()}
                if not (colmap["postcode"] and colmap["address1"] and colmap["fa"]):
                    print(f"error: missing POSTCODE/ADDRESS1/TOTAL_FLOOR_AREA columns; got {list(row.keys())[:12]}")
                    sys.exit(2)
            pc = _norm_pc(row.get(colmap["postcode"]))
            if not pc:
                continue
            if OUTCODES and pc.split(" ")[0] not in OUTCODES:
                continue
            try:
                fa = int(round(float(row.get(colmap["fa"]) or 0)))
            except (TypeError, ValueError):
                continue
            if fa <= 5 or fa > 2000:           # skip blanks / obvious garbage
                continue
            paon = _paon(row.get(colmap["address1"]))
            if not paon:
                continue
            key = f"{pc}|{paon}".upper()
            date = ((colmap["lodged"] and row.get(colmap["lodged"]))
                    or (colmap["inspected"] and row.get(colmap["inspected"])) or "")
            prev = certs.get(key)
            if prev and str(prev.get("date", "")) > str(date):
                continue                        # keep the most recent certificate
            try:
                rooms = int(float(row.get(colmap["rooms"]) or 0)) or None
            except (TypeError, ValueError):
                rooms = None
            certs[key] = {
                "fa": fa,
                "form": (row.get(colmap["form"]) or "").strip() if colmap["form"] else "",
                "type": (row.get(colmap["type"]) or "").strip() if colmap["type"] else "",
                "rooms": rooms,
                "date": (date or "")[:10],
            }
            kept += 1

    with gzip.open(out, "wt") as f:
        json.dump({"certs": certs}, f)
    print(f"read {seen:,} certificate rows, wrote {len(certs):,} unique properties -> {out}")
    if not certs:
        print("warning: 0 properties written - check the CSV columns and that the file covers your area.")


if __name__ == "__main__":
    main()
