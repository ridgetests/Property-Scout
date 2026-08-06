#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_barn_planning.py -- mine Class-Q / agricultural-conversion planning applications
in the Farnham bowl, and flag the warmest leads.

THE LEAD TYPES (from Spec_Latent_Opportunity_Engine.md, signal 1):
  * "approved but not implemented"  -- prior approval GRANTED, nothing built. The hottest
    lead: the legal hurdle is cleared and the owner stopped (money/appetite/expertise).
  * "refused on a fixable point"    -- owner thinks the door is shut, so it's cheap; a
    developer who knows the remedy (access, a structural report) can unlock it.

SOURCE: PlanIt (https://www.planit.org.uk/api/applics/json), already used per-point in
run.py. Here we do an AREA mine of the bowl. PlanIt has NO app_type for Class Q, so we
use a bbox + a few free-text `search` terms and dedupe on `name`. PlanIt 429s after a
burst, so requests are SPACED (10s, Gazette-style) and behind run.py's circuit-breaker.
NO commencement/built data exists, so "dormant" is inferred from decided_date age.

Heavy/occasional: run as the "Build barn planning leads" workflow button, NOT nightly.

  python3 make_barn_planning.py
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime

# PlanIt sometimes records the LOCAL AUTHORITY's own office as the application address
# (e.g. "Waverley Borough Council, The Burys, Godalming") -- that's not a barn. Drop it.
_LPA_ADDR_RE = re.compile(
    r"\b(borough|district|county|town|parish)\s+council\b|council offices|"
    r"civic (offices|centre)|\bthe burys\b", re.IGNORECASE)

OUTPUT = os.environ.get("PS_OUTPUT", "docs/barn_planning.json")
BBOX = os.environ.get("PS_BBOX", "-0.93,51.09,-0.58,51.31")   # lng_min,lat_min,lng_max,lat_max
CLASSQ_START = "2014-04-06"        # Class Q came into force
REQUEST_DELAY = float(os.environ.get("PS_PLANIT_DELAY", "10"))   # seconds between calls
MAX_PAGES_PER_SEARCH = 4
SEARCHES = ["class q", "prior approval agricultural", "agricultural dwellinghouse",
            "conversion of agricultural building"]

APPROVED = {"permitted", "conditions"}
REFUSED = {"rejected"}
PENDING = {"undecided", "referred", "unresolved"}


def _is_classq(rec):
    """Client-side confirmation that a returned application really is a Class-Q /
    agricultural-conversion case (the free-text `search` can match loosely)."""
    blob = " ".join(str(rec.get(k) or "") for k in
                    ("description", "app_type", "address")).lower()
    return ("class q" in blob or "prior approval" in blob
            or ("agricultural" in blob and any(w in blob for w in
                ("dwelling", "residential", "conversion", "dwellinghouse", "house")))
            or ("barn" in blob and ("conversion" in blob or "dwelling" in blob)))


def _coords(rec, run):
    """(lat, lon) for an application. PlanIt gives location_x/location_y (WGS84
    lon/lat for GB); if those aren't sane, fall back to geocoding the postcode."""
    x, y = rec.get("location_x"), rec.get("location_y")
    try:
        x, y = float(x), float(y)
        if -8.0 <= x <= 2.0 and 49.0 <= y <= 61.0:
            return y, x           # location_x = lon, location_y = lat
    except (TypeError, ValueError):
        pass
    pc = (rec.get("postcode") or "").strip()
    if pc:
        g = run.geocode([pc]).get(pc)
        if g:
            return g
    return None, None


def _age_years(decided):
    try:
        d = datetime.strptime(str(decided)[:10], "%Y-%m-%d").date()
        return round((date.today() - d).days / 365.25, 1)
    except Exception:
        return None


def _classify(rec):
    st = (rec.get("app_state") or "").lower()
    age = _age_years(rec.get("decided_date"))
    if st in APPROVED:
        if age is not None and age >= 1.0:
            # cleared the legal hurdle, nothing since -> the hottest lead. Past ~3yr the
            # Class-Q completion window has lapsed (still a buyable consent-in-principle).
            return "approved_dormant" if age < 3.0 else "approved_lapsed"
        return "approved_recent"
    if st in REFUSED:
        return "refused"          # possibly fixable -- surface the reason for a human
    if st in PENDING:
        return "pending"
    return "other"                # withdrawn / referred / etc.


def main():
    import requests
    import run

    print("bbox: %s | Class-Q window from %s" % (BBOX, CLASSQ_START))
    conn = run.db_connect()
    seen = {}       # name -> record (dedupe across searches)
    today = str(date.today())
    URL = "https://www.planit.org.uk/api/applics/json"
    HDR = {"User-Agent": run._UA, "Accept": "application/json"}
    cy, cx, krad = 51.20, -0.755, 14      # bowl centre + radius, fallback if bbox is rejected
    stats = {"req": 0, "win": None}       # request count + the parameter shape that worked

    def _variants(term, page):
        # fullest first, then progressively simpler; last resort swaps bbox -> lat/lng/krad
        # (the shape run.py already uses successfully). 'select'/'sort' are dropped entirely
        # since an unsupported param is exactly what returns 400.
        base = {"search": term, "pg_sz": 100, "page": page}
        return [
            {**base, "bbox": BBOX, "start_date": CLASSQ_START, "end_date": today},
            {**base, "bbox": BBOX},
            {**base, "lat": cy, "lng": cx, "krad": krad, "start_date": CLASSQ_START, "end_date": today},
            {**base, "lat": cy, "lng": cx, "krad": krad},
        ]

    def _fetch(term, page):
        """Response JSON, self-discovering a working parameter shape once. A 400 means an
        unsupported param (not a rate-limit) -> try a simpler variant. A 429 parks PlanIt."""
        vs = _variants(term, page)
        order = [stats["win"]] if stats["win"] is not None else list(range(len(vs)))
        for i in order:
            if run._dead("planit"):
                return None
            try:
                r = requests.get(URL, params=vs[i], headers=HDR, timeout=30)
                stats["req"] += 1
                if r.status_code == 400:
                    print("  variant %d -> 400 (unsupported params); trying simpler" % i)
                    time.sleep(REQUEST_DELAY)
                    continue
                r.raise_for_status()
                if stats["win"] is None:
                    print("  using parameter variant %d (%s)"
                          % (i, "bbox" if "bbox" in vs[i] else "lat/lng/krad"))
                stats["win"] = i
                return r.json()
            except Exception as ex:
                print("  %r p%d variant %d failed: %s" % (term, page, i, ex))
                if run._throttled(ex):
                    run._kill("planit", "429")
                    return None
                time.sleep(REQUEST_DELAY)
        return None

    for term in SEARCHES:
        if run._dead("planit"):
            print("  planit parked -- stopping"); break
        page = 1
        while page <= MAX_PAGES_PER_SEARCH:
            body = _fetch(term, page)
            if body is None:
                break
            recs = body.get("records", []) or []
            total = body.get("total", len(recs))
            for rec in recs:
                nm = rec.get("name") or rec.get("uid")
                if nm and nm not in seen:
                    seen[nm] = rec
            print("  search %r page %d: %d recs (total %s)" % (term, page, len(recs), total))
            if not recs or page * 100 >= (total or 0):
                break
            page += 1
            time.sleep(REQUEST_DELAY)
        time.sleep(REQUEST_DELAY)

    requests_made = stats["req"]

    # confirm Class-Q, locate, keep only in-bowl, classify
    leads, dropped_type, dropped_area, dropped_lpa = [], 0, 0, 0
    for rec in seen.values():
        if _LPA_ADDR_RE.search(str(rec.get("address") or "")):
            dropped_lpa += 1        # the council's own office address, not a barn
            continue
        if not _is_classq(rec):
            dropped_type += 1
            continue
        lat, lon = _coords(rec, run)
        if lat is None or not run._in_polygon(lat, lon, run.AREA_POLYGON):
            dropped_area += 1
            continue
        cat = _classify(rec)
        leads.append({
            "name": rec.get("name"), "address": rec.get("address"),
            "postcode": rec.get("postcode"), "area_name": rec.get("area_name"),
            "description": (rec.get("description") or "")[:280],
            "app_state": rec.get("app_state"), "app_type": rec.get("app_type"),
            "start_date": rec.get("start_date"), "decided_date": rec.get("decided_date"),
            "age_years": _age_years(rec.get("decided_date")),
            "category": cat, "lat": round(lat, 6), "lon": round(lon, 6),
            "url": rec.get("url") or rec.get("link"),
            "has_followon": bool(rec.get("associated_id")),
        })

    # hottest first: dormant approvals, then lapsed, then refused, then the rest
    order = {"approved_dormant": 0, "approved_lapsed": 1, "refused": 2,
             "approved_recent": 3, "pending": 4, "other": 5}
    leads.sort(key=lambda x: (order.get(x["category"], 9), -(x["age_years"] or 0)))

    from collections import Counter
    cats = Counter(x["category"] for x in leads)
    print("\nPLANIT MINE (%d requests)" % requests_made)
    print("  Class-Q applications in bowl : %d" % len(leads))
    print("  dropped (not Class-Q / area / council-office) : %d / %d / %d"
          % (dropped_type, dropped_area, dropped_lpa))
    for c in ("approved_dormant", "approved_lapsed", "refused", "approved_recent", "pending"):
        print("  %-18s : %d" % (c, cats.get(c, 0)))

    if run._dead("planit") and not leads:
        sys.exit("\nABORT: PlanIt was rate-limited before any data came back. "
                 "Nothing written -- try again later (it heals).")

    out = {"leads": leads, "counts": dict(cats), "requests_made": requests_made,
           "planit_parked": run._dead("planit"),
           "note": ("Class-Q / agricultural-conversion applications, PlanIt. "
                    "'approved_dormant' = permitted 1-3 yrs ago, nothing built (hottest). "
                    "'approved_lapsed' = >3 yrs (consent may have expired but is buyable). "
                    "'refused' = possibly fixable -- read the reason. Verify each on the "
                    "council portal; this is triage, not planning advice.")}
    with open(OUTPUT, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print("\nwrote %s (%d leads, %.0f KB)"
          % (OUTPUT, len(leads), os.path.getsize(OUTPUT) / 1024.0))
    print("\nHOTTEST 12")
    for x in leads[:12]:
        print("  %-18s %4s yr  %-8s  %s"
              % (x["category"], x["age_years"], (x["app_state"] or "")[:8],
                 (x["address"] or x["postcode"] or "")[:44]))


if __name__ == "__main__":
    main()
