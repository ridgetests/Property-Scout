#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_carehomes.py -- build a local list of care-home postcodes from the CQC register,
so probate leads that are actually care homes get dropped automatically.

WHY: a probate notice often gives only a care home's STREET ADDRESS (no "care home"
wording), so run.py's text filter can't catch it (e.g. "43 Waverley Lane" is Waverley
Grange Care Home). The authoritative source is the Care Quality Commission's register of
every active care home in England. Downloaded once, filtered to our area, it catches them
all -- no manual list to maintain.

SOURCE: CQC "Care directory with filters" bulk file, HSCA_Active_Locations.ods (OpenDocument,
~23 MB, keyless, no account). The `Care home?` column == "Y" is the definitive flag.
Filename embeds the month and changes monthly, so we discover the current link from the
CQC page, with constructed-URL fallbacks. Parsed with pandas + odfpy.

Heavy/occasional: run as the "Build care-home list" workflow button, NOT the nightly cron.

  pip install pandas odfpy
  python3 make_carehomes.py
"""

import gzip
import io
import json
import os
import re
import sys
from datetime import date
from urllib.parse import urljoin

OUTPUT = os.environ.get("PS_OUTPUT", "care_homes_region.json.gz")
PAGE = "https://www.cqc.org.uk/about-us/transparency/using-cqc-data"
# CQC's WAF 403s a bare python-requests UA -- present a browser-like one.
UA = ("Mozilla/5.0 (compatible; PropertyScout care-home filter; "
      "+personal property research; contact: heystevenridgeway@gmail.com)")
# Farnham / Alton / Fleet / Bordon postcode districts (outward codes).
DISTRICTS = {"GU7", "GU8", "GU9", "GU10", "GU30", "GU33", "GU34", "GU35", "GU51", "GU52",
             "RG29"}
SHEET = "HSCA_Active_Locations"
_MONTHS = ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def _candidate_urls():
    """Constructed monthly URLs (current month back a few), as a fallback if the page
    scrape fails. Pattern: .../<YYYY>-<MM>/01_<Month>_<YYYY>_HSCA_Active_Locations.ods"""
    urls = []
    y, m = date.today().year, date.today().month
    for _ in range(6):
        urls.append("https://www.cqc.org.uk/sites/default/files/"
                    "%04d-%02d/01_%s_%04d_HSCA_Active_Locations.ods"
                    % (y, m, _MONTHS[m - 1], y))
        m -= 1
        if m == 0:
            m = 12; y -= 1
    return urls


def _download(session):
    """Return the .ods bytes. Discovers the current link from the CQC page, then falls
    back to constructed monthly URLs. Fails loudly if none work."""
    tried = []
    # 1) scrape the hosting page for the current HSCA_Active_Locations.ods link
    try:
        r = session.get(PAGE, timeout=40)
        if r.ok:
            links = re.findall(r'href=["\']([^"\']*HSCA_Active_Locations\.ods)["\']',
                               r.text, re.I)
            for href in links:
                url = urljoin(PAGE, href)
                if url not in tried:
                    tried.insert(0, url)
        else:
            print("  page fetch -> HTTP %d" % r.status_code)
    except Exception as e:
        print("  page scrape failed: %s" % e)
    tried += [u for u in _candidate_urls() if u not in tried]

    for url in tried:
        try:
            print("  trying %s" % url)
            r = session.get(url, timeout=120)
            if r.status_code == 200 and r.content[:2] == b"PK":   # .ods is a zip
                print("  got %.1f MB" % (len(r.content) / 1e6))
                return r.content
            print("    -> HTTP %d (%d bytes)" % (r.status_code, len(r.content)))
        except Exception as e:
            print("    failed: %s" % e)
    sys.exit("ABORT: could not download HSCA_Active_Locations.ods from CQC. "
             "The monthly filename may have changed; check %s" % PAGE)


def main():
    import requests
    try:
        import pandas as pd
    except Exception as e:
        sys.exit("pandas/odfpy not available (%s). `pip install pandas odfpy`." % e)

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    data = _download(session)

    print("parsing %s sheet..." % SHEET)
    df = pd.read_excel(io.BytesIO(data), sheet_name=SHEET, engine="odf")
    print("  %d rows x %d cols" % (df.shape[0], df.shape[1]))

    need = ["Care home?", "Location Name", "Location Postal Code"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        sys.exit("ABORT: expected columns missing: %s (CQC may have renamed them). "
                 "Have: %s" % (missing, list(df.columns)[:30]))

    homes = df[df["Care home?"].astype(str).str.upper().str.strip() == "Y"]
    if "Dormant (Y/N)" in df.columns:
        homes = homes[homes["Dormant (Y/N)"].astype(str).str.upper().str.strip() != "Y"]
    print("  %d care homes in England" % len(homes))

    out_pcs, carehomes = set(), []
    for _, row in homes.iterrows():
        pc = str(row["Location Postal Code"] or "").strip().upper()
        pc = re.sub(r"\s+", " ", pc)
        if not pc or " " not in pc:
            continue
        outward = pc.split(" ")[0]
        if outward not in DISTRICTS:
            continue
        out_pcs.add(pc)
        carehomes.append({"name": str(row["Location Name"] or "").strip(), "postcode": pc})

    print("\nCARE HOMES IN AREA: %d (postcodes: %d)" % (len(carehomes), len(out_pcs)))
    if not carehomes:
        sys.exit("ABORT: 0 care homes matched the area districts %s -- filter or column "
                 "handling is wrong. Writing nothing." % sorted(DISTRICTS))

    out = {"postcodes": sorted(out_pcs),
           "carehomes": sorted(carehomes, key=lambda x: x["postcode"]),
           "districts": sorted(DISTRICTS),
           "source": "CQC HSCA_Active_Locations (Care home? == Y)",
           "note": "Postcodes of registered care homes in the area; used to drop probate "
                   "leads that are actually care homes."}
    with gzip.open(OUTPUT, "wt") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print("wrote %s (%d postcodes, %.0f KB)"
          % (OUTPUT, len(out_pcs), os.path.getsize(OUTPUT) / 1024.0))
    print("\nSAMPLE")
    for c in carehomes[:12]:
        print("  %-9s %s" % (c["postcode"], c["name"][:48]))


if __name__ == "__main__":
    main()
