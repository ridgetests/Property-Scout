#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_distress.py -- pre-market DISTRESS signals from The Gazette (the repossession angle).

WHY THIS EXISTS
---------------
There is no open feed of repossessions -- courts and lenders don't publish them. But The
Gazette (the official public record) publishes the LEADING indicators, and this is the
legal way to catch them before an agent is involved:
  * LPA / fixed-charge RECEIVER appointments -- a lender appointing a receiver over a
    property is the step right before a forced sale, and the notice NAMES the property.
  * CORPORATE winding-up / administration / liquidation -- a company that may own local
    property, in trouble.

DELIBERATELY EXCLUDES individual bankruptcy / IVAs. Those are living people in financial
distress: aggregating them carries data-protection duties (and an ethical line) this tool
won't take on. Companies and charged-property notices only.

PRIVACY: exactly the discipline the probate feed already uses -- we store ONLY the
postcode, the notice TYPE, and the link to the already-public Gazette notice. No names,
no notice text is aggregated into the file. You open the public notice on demand.

SOURCE: The Gazette insolvency notices (data.json), same host/shape as the probate feed
in run.py. Free, no key, Open Government Licence. Their crawl-delay is 1 request / 10s;
this makes only a couple of calls. Manual button, NOT the nightly.

Writes: docs/distress.json  [{type, postcode, url, date, lat?, lon?}]
  python3 make_distress.py
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

OUTPUT = os.environ.get("PS_OUTPUT", "docs/distress.json")
LOCATION = os.environ.get("PS_DISTRESS_LOCATION", "Farnham")      # Gazette accepts a town/postcode
RADIUS_MI = int(os.environ.get("PS_DISTRESS_RADIUS", "8"))
LOOKBACK_DAYS = int(os.environ.get("PS_DISTRESS_LOOKBACK", "365"))
FEED = "https://www.thegazette.co.uk/insolvency/notice/data.json"

# Classify by notice text. Keep property/company distress; drop individuals entirely.
_INDIVIDUAL = re.compile(r"bankruptcy order|bankruptcy petition|individual voluntary "
                         r"arrangement|\bIVA\b|debt relief order", re.I)
_RECEIVER = re.compile(r"\breceiver\b|law of property act 1925|fixed[- ]charge", re.I)
_ADMIN = re.compile(r"\badministrat(?:or|ion)\b", re.I)
_WINDUP = re.compile(r"winding[- ]up|liquidat|creditors'? voluntary", re.I)


def _classify(text):
    """Notice type for the map, or None to drop (individual / not property-relevant)."""
    if _INDIVIDUAL.search(text):
        return None
    if _RECEIVER.search(text):
        return "receiver"            # strongest: names the charged property
    if _ADMIN.search(text):
        return "administration"
    if _WINDUP.search(text):
        return "winding_up"
    return None


def _notice_url(e, pid):
    lnk = e.get("link")
    if isinstance(lnk, dict):
        return lnk.get("@href") or lnk.get("href") or ""
    if isinstance(lnk, list):
        for L in lnk:
            if isinstance(L, dict) and L.get("@rel", "alternate") == "alternate":
                u = L.get("@href") or L.get("href") or ""
                if u:
                    return u
    if isinstance(lnk, str):
        return lnk
    return "https://www.thegazette.co.uk/notice/%s" % pid


def main():
    import requests
    import run

    since = (datetime.now(timezone.utc).date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    params = {"location-postcode-1": LOCATION, "location-distance-1": RADIUS_MI,
              "start-publish-date": since, "results-page-size": 100, "sort-by": "latest-date"}
    if run._dead("gazette"):
        sys.exit("ABORT: gazette breaker already tripped; run again later.")
    try:
        r = requests.get(FEED, params=params, headers={"User-Agent": run._UA}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        if run._throttled(e):
            run._kill("gazette", "429/403")
        sys.exit("ABORT: Gazette insolvency fetch failed (%s). Writing nothing." % e)

    entries = data.get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    print("insolvency notices near %s (%dmi, since %s): %d"
          % (LOCATION, RADIUS_MI, since, len(entries)))

    staged, pcs = [], []
    dropped_ind = dropped_area = 0
    for e in entries:
        blob = (run._strip_tags(e.get("title") or "") + " "
                + run._strip_tags(e.get("content") or ""))
        cat = _classify(blob)
        if cat is None:
            dropped_ind += 1
            continue
        m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", blob)
        pc = m.group(1).upper() if m else ""
        if pc and pc.split()[0] in run.EXCLUDE_DISTRICTS:
            dropped_area += 1
            continue
        pid = (e.get("id") or "").rsplit("/", 1)[-1]
        pub = (e.get("updated") or e.get("published") or "")[:10]
        staged.append({"type": cat, "postcode": pc, "url": _notice_url(e, pid), "date": pub})
        if pc:
            pcs.append(pc)

    loc = run.geocode(pcs) if pcs else {}
    out = []
    for s in staged:
        ll = loc.get(s["postcode"])
        if ll:
            s["lat"], s["lon"] = round(ll[0], 6), round(ll[1], 6)
        out.append(s)

    from collections import Counter
    out.sort(key=lambda s: (0 if s.get("lat") else 1, s.get("date") or ""), reverse=False)
    print("kept %d | dropped %d individual/other | dropped %d out-of-area"
          % (len(out), dropped_ind, dropped_area))
    print("by type:", dict(Counter(s["type"] for s in out)))

    payload = {
        "distress": out, "count": len(out),
        "source": "The Gazette insolvency notices (OGL). Receiver appointments name a "
                  "charged property; winding-up / administration flag a company that may "
                  "own local property. Individual bankruptcy / IVA excluded. Postcode-level "
                  "-- open the linked notice to verify. Not advice.",
    }
    with open(OUTPUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print("wrote %s (%d, %.0f KB)" % (OUTPUT, len(out), os.path.getsize(OUTPUT) / 1024.0))


if __name__ == "__main__":
    main()
