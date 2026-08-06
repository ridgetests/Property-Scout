#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_barn_candidates.py -- rank candidate Class-Q barns from vectors, then hard-gate
on designation.

READS  (repo root):
    building_polygons.json.gz   building OUTLINES + OSM use tags (make_footprints.py)
    plots_waverley.json.gz      HMLR INSPIRE parcels (already in the repo)

WRITES:
    barn_candidates.json        ranked shortlist (small, human-readable, committable)

WHAT IT DOES
------------
1. DETECT/SCORE from geometry (no network): every building is scored on the barn
   signature -- an OSM agricultural tag (barn/farm_auxiliary/stable...), a Class-Q
   size (150-1000 m2), a simple + elongated shape (shed, not a house with wings),
   sitting on a FIELD-scale parcel, and ISOLATED from settlement.
2. GATE on designation (network, top-N only): Class Q is dead in a National
   Landscape (AONB), Conservation Area, National Park, SSSI, or on a listed
   building. We hard-flag those. Uses run.fetch_constraints -- cached in SQLite +
   behind the circuit-breaker, so re-runs are cheap and one 429 parks the source.

Designation is checked for the top-N geometric candidates ONLY (default 200), so a
run makes at most N constraint calls -- never one per building. Heavy: run as the
"Build barn candidates" workflow button, NOT the nightly cron.

  python3 make_barn_candidates.py                 # full run (with designation)
  python3 make_barn_candidates.py --no-designation # geometry only (offline test)
"""

import gzip
import json
import math
import os
import sys

BUILDINGS = os.environ.get("PS_BUILDINGS", "building_polygons.json.gz")
OUTPUT = os.environ.get("PS_OUTPUT", "docs/barn_candidates.json")   # in docs/ so the viewer can load it
DESIGNATION_TOP_N = int(os.environ.get("PS_DESIGNATION_N", "200"))

# Class Q: up to 1,000 m2 converted; below ~150 m2 you're in stable/garage territory.
AREA_MIN, AREA_MAX = 150.0, 1000.0
PARCEL_FIELD_SCALE = 5000.0      # a field, not a garden
DENSITY_RADIUS_M = 150.0         # isolation radius
MAX_OUTPUT = 200                 # matches DESIGNATION_TOP_N so every output cand is gated

# OSM building/use tags that genuinely mean "agricultural" (Class-Q relevant).
# Deliberately TIGHT: 'shed'/'garage'/'outbuilding' are usually domestic, and
# 'warehouse'/'retail'/'industrial' are commercial -- none are Class-Q agricultural.
AGRI_TAGS = {"barn", "farm_auxiliary", "cowshed", "cowhouse", "stable", "stables",
             "sty", "farm", "agricultural", "greenhouse", "glasshouse", "silo"}

# Designations that KILL Class Q (Article 2(3) land + listed/SSSI). Green Belt is FINE.
CLASSQ_EXCLUSIONS = {"area-of-outstanding-natural-beauty", "conservation-area",
                     "national-park", "site-of-special-scientific-interest",
                     "listed-building"}

M_PER_DEG_LAT = 110540.0
M_PER_DEG_LON_EQ = 111320.0


class Grid:
    """Uniform grid spatial index (cheap, adequate at this scale)."""
    def __init__(self, cell_deg=0.005):
        self.cell = cell_deg
        self.buckets = {}

    def _key(self, lat, lon):
        return (int(lat / self.cell), int(lon / self.cell))

    def add_point(self, lat, lon, payload):
        self.buckets.setdefault(self._key(lat, lon), []).append(payload)

    def add_bbox(self, latmin, latmax, lonmin, lonmax, payload):
        i0, j0 = self._key(latmin, lonmin)
        i1, j1 = self._key(latmax, lonmax)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                self.buckets.setdefault((i, j), []).append(payload)

    def near(self, lat, lon, rings=1):
        i0, j0 = self._key(lat, lon)
        out = []
        for i in range(i0 - rings, i0 + rings + 1):
            for j in range(j0 - rings, j0 + rings + 1):
                out.extend(self.buckets.get((i, j), ()))
        return out


def _pip(lat, lon, ring):
    """Ray-cast point-in-polygon; ring is [[lat,lon],...]."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        yi, xi = ring[i][0], ring[i][1]
        yj, xj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            denom = (yj - yi)
            if denom != 0.0 and lon < (xj - xi) * (lat - yi) / denom + xi:
                inside = not inside
        j = i
    return inside


def _shape_metrics(ring, lat0):
    """Elongation (long:short axis, via PCA) and simplicity (vertex count) of an
    outline. A shed is elongated and simple; a house is squarer and complex."""
    k = M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))
    pts = [((lon) * k, (lat) * M_PER_DEG_LAT) for lat, lon in ring]
    # drop a duplicate closing vertex if present
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    n = len(pts)
    if n < 3:
        return 1.0, n
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts) / n
    syy = sum((p[1] - my) ** 2 for p in pts) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts) / n
    # eigenvalues of the 2x2 covariance
    tr, det = sxx + syy, sxx * syy - sxy * sxy
    disc = max(0.0, tr * tr / 4 - det)
    l1 = tr / 2 + math.sqrt(disc)
    l2 = tr / 2 - math.sqrt(disc)
    elong = math.sqrt(l1 / l2) if l2 > 1e-9 else 6.0
    return min(elong, 6.0), n


def load_gz(path, key):
    if not os.path.exists(path):
        sys.exit("MISSING %s -- build it first (see the workflow)." % path)
    with gzip.open(path, "rt") as fh:
        return json.load(fh).get(key, [])


def main():
    no_desig = "--no-designation" in sys.argv

    buildings = load_gz(BUILDINGS, "buildings")
    parcels = load_gz("plots_waverley.json.gz", "parcels")
    print("buildings : %d" % len(buildings))
    print("parcels   : %d" % len(parcels))

    # density index over ALL building centres (isolation needs the full set)
    dens = Grid(cell_deg=0.0025)
    for b in buildings:
        c = b.get("c")
        if c:
            dens.add_point(c[0], c[1], c)

    # field-scale parcel index
    pidx = Grid(cell_deg=0.005)
    fields = 0
    for p in parcels:
        r, bb = p.get("r"), p.get("b")
        if not r or len(r) < 4 or not bb or p.get("a", 0) < PARCEL_FIELD_SCALE:
            continue
        fields += 1
        pidx.add_bbox(bb[0], bb[1], bb[2], bb[3], p)
    print("field parcels (>=%dm2): %d" % (PARCEL_FIELD_SCALE, fields))

    candidates = []
    in_band = on_field = 0
    for b in buildings:
        a = b.get("a", 0)
        c = b.get("c")
        u = (b.get("u") or "").lower()
        agri = u in AGRI_TAGS
        # size band: agri-tagged buildings are kept a bit wider (a tagged barn just
        # over 1,000 m2 is still a play -- partly convertible)
        lo, hi = (AREA_MIN, AREA_MAX if not agri else 1500.0)
        if not c or not (lo <= a <= hi):
            continue
        in_band += 1
        lat, lon = c[0], c[1]

        # host field parcel
        host = None
        for p in pidx.near(lat, lon, rings=1):
            bb = p["b"]
            if bb[0] <= lat <= bb[1] and bb[2] <= lon <= bb[3] and _pip(lat, lon, p["r"]):
                if host is None or p["a"] > host["a"]:
                    host = p
        # a building must be on a field OR carry an agricultural tag to qualify
        if host is None and not agri:
            continue
        on_field += 1 if host is not None else 0

        # isolation
        k = M_PER_DEG_LON_EQ * math.cos(math.radians(lat))
        neighbours = 0
        for oc in dens.near(lat, lon, rings=1):
            if oc is c:
                continue
            dx = (oc[1] - lon) * k
            dy = (oc[0] - lat) * M_PER_DEG_LAT
            if math.hypot(dx, dy) <= DENSITY_RADIUS_M:
                neighbours += 1

        elong, nverts = _shape_metrics(b.get("r") or [], lat)

        # ---- score (0-1) --------------------------------------------------
        s_tag = 1.0 if agri else 0.0
        s_area = min(1.0, (a - AREA_MIN) / 450.0) if a < 600 else 1.0     # mid/large sheds peak
        s_parcel = (min(1.0, math.log10(host["a"] / PARCEL_FIELD_SCALE + 1.0) / 1.4)
                    if host else 0.3)
        s_iso = max(0.0, 1.0 - neighbours / 12.0)
        s_simple = max(0.0, 1.0 - max(0, nverts - 4) / 12.0)             # 4-8 verts = shed
        s_elong = min(1.0, (elong - 1.0) / 2.0)                          # 3:1+ = very shed-like
        s_shape = 0.5 * s_simple + 0.5 * s_elong

        score = (0.33 * s_tag + 0.14 * s_area + 0.20 * s_parcel
                 + 0.16 * s_iso + 0.17 * s_shape)

        candidates.append({
            "lat": round(lat, 6), "lon": round(lon, 6),
            "area_m2": int(a),
            "parcel_m2": int(host["a"]) if host else None,
            "neighbours_150m": neighbours,
            "use_tag": u or None,
            "tier": "tagged-agri" if agri else "geometry",
            "elongation": round(elong, 2), "vertices": nverts,
            "score": round(score, 4),
            "class_q_floorspace_m2": int(min(a, 1000)),
            # Class Q ceiling: 10 dwellings / 1,000 m2 total / 150 m2 each
            "max_dwellings": min(10, max(1, int(min(a, 1000) // 150))),
        })

    candidates.sort(key=lambda x: -x["score"])
    print("\nFUNNEL")
    print("  buildings                    : %d" % len(buildings))
    print("  in Class-Q size band         : %d" % in_band)
    print("  ...on a field / agri-tagged  : %d" % len(candidates))
    for thr in (0.5, 0.6, 0.7, 0.8):
        print("  ...score >= %.1f              : %d"
              % (thr, sum(1 for x in candidates if x["score"] >= thr)))
    print("  tagged-agri in shortlist     : %d"
          % sum(1 for x in candidates if x["tier"] == "tagged-agri"))

    shortlist = candidates[:MAX_OUTPUT]

    # --- designation gate (network; top-N only) ---
    gated = 0
    if not no_desig and shortlist:
        import time
        import run
        conn = run.db_connect()
        n = min(DESIGNATION_TOP_N, len(shortlist))
        print("\nDESIGNATION GATE (top %d; cached + circuit-broken)" % n)
        for i, cand in enumerate(shortlist[:n]):
            if run._dead("planning_data"):
                print("  planning_data parked (429/403) -- remaining left unchecked")
                break
            con = run.fetch_constraints(cand["lat"], cand["lon"], conn)
            if not con or "datasets" not in con:
                # fetch failed (e.g. a 502) or the source parked -- do NOT assume
                # eligible; mark it unchecked so the viewer shows "verify".
                cand["designation"] = {"eligible": None, "checked": False}
            else:
                ds = set(con.get("datasets") or [])
                excl = sorted(ds & CLASSQ_EXCLUSIONS)
                cand["designation"] = {
                    "eligible": len(excl) == 0, "checked": True,
                    "excluded_by": [run._CONSTRAINT_LABEL.get(d, d) for d in excl],
                    "green_belt": "green-belt" in ds,
                    "all": [run._CONSTRAINT_LABEL.get(d, d) for d in sorted(ds)],
                }
            gated += 1
            time.sleep(0.4)     # be gentle on planning.data.gov.uk
        eligible = sum(1 for c in shortlist if c.get("designation", {}).get("eligible"))
        print("  checked %d; Class-Q ELIGIBLE (not in AONB/CA/NP/SSSI/listed): %d" % (gated, eligible))

    out = {
        "candidates": shortlist,
        "generated_from": ["building_polygons.json.gz (OSM/ODbL)", "plots_waverley.json.gz (HMLR INSPIRE)"],
        "designation_checked": gated,
        "params": {"area_min_m2": AREA_MIN, "area_max_m2": AREA_MAX,
                   "parcel_field_scale_m2": PARCEL_FIELD_SCALE,
                   "density_radius_m": DENSITY_RADIUS_M},
        "warnings": [
            "Class Q eligibility is checked against LIVE designation data. It is a "
            "triage flag, not planning advice -- verify before acting.",
            "Surrey Hills National Landscape is being EXTENDED over parts of this bowl "
            "(Frensham/Dockenfield/Rowledge/Wey Valley). If confirmed, Class Q is "
            "extinguished there. Re-check the Order before any decision.",
            "Structural soundness (the Hibbitt test) and access cannot be judged from "
            "this data -- imagery + a survey are required.",
        ],
    }
    with open(OUTPUT, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print("\nwrote %s (%d candidates, %.0f KB)"
          % (OUTPUT, len(shortlist), os.path.getsize(OUTPUT) / 1024.0))

    print("\nTOP 15")
    print("  %-8s %-9s %7s %9s %5s %-14s %5s" %
          ("lat", "lon", "m2", "parcel", "nbrs", "tag/tier", "score"))
    for x in shortlist[:15]:
        elig = x.get("designation", {}).get("eligible")
        mark = "" if elig is None else (" OK" if elig else " X")
        print("  %-8.5f %-9.5f %7d %9s %5d %-14s %5.2f%s" %
              (x["lat"], x["lon"], x["area_m2"],
               (x["parcel_m2"] if x["parcel_m2"] else "-"),
               x["neighbours_150m"], (x["use_tag"] or x["tier"])[:14],
               x["score"], mark))


if __name__ == "__main__":
    main()
