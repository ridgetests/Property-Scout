"""
The scoring model. Six signals, each capped by config.WEIGHTS, summed and
normalised to a 0-100 headline. Every function takes the enriched Property
and returns (points, note) so the breakdown stays explainable in the UI.

These are starting heuristics. The intent is that you tune them against your
own accept/reject decisions over time — that feedback loop is the real edge.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from config import (
    WEIGHTS, MAX_TOTAL, RENO_RATE_PER_M2, EXTENSION_ALLOWANCE,
    LOW_COMP_THRESHOLD, THIN_CHANNELS, SEARCH,
)

MOTIVATION_TERMS = [
    "executor", "probate", "estate of", "sold as seen", "no onward chain",
    "no chain", "cash buyers", "deceased", "reluctant", "must sell",
]
STRUCTURAL_TERMS = [
    "planning permission", "pp granted", "development potential", "scope to",
    "potential to", "annexe", "annex", "outbuilding", "workshop", "barn",
    "in need of modernisation", "needs updating", "requires renovation",
]


def _has(text: str, terms: list[str]) -> list[str]:
    t = (text or "").lower()
    return [term for term in terms if term in t]


def _reno_rate(prop) -> int:
    epc = prop.enrichment.get("epc") or {}
    rating = (epc.get("rating") or "").upper()
    if rating in ("F", "G"):
        return RENO_RATE_PER_M2["poor"]
    if rating in ("E", "D"):
        return RENO_RATE_PER_M2["dated"]
    return RENO_RATE_PER_M2["fair"]


def _renovated_comp(prop) -> int | None:
    """Median renovated comp, else local £/m² estimate against floor area."""
    reno = [c["price"] for c in prop.comps if c.get("renovated")]
    if reno:
        reno.sort()
        return reno[len(reno) // 2]
    epc = prop.enrichment.get("epc") or {}
    fa = epc.get("floor_area_m2")
    if fa and prop.comps:
        ppm2 = sorted(c["price"] / c["m2"] for c in prop.comps if c.get("m2"))
        if ppm2:
            return int(ppm2[len(ppm2) // 2] * fa * 1.15)
    return None


# ---- individual signals ---------------------------------------------------

def score_equity(prop):
    cap = WEIGHTS["equity_residual"]
    comp = _renovated_comp(prop)
    epc = prop.enrichment.get("epc") or {}
    fa = epc.get("floor_area_m2")
    if not comp or not fa:
        prop.enrichment.setdefault("equity", {})
        return 0, "Insufficient comp / floor-area data to estimate equity."
    reno_cost = int(fa * _reno_rate(prop))
    if _has(prop.description_raw, ["extension", "extend"]) or prop.property_type == "bungalow":
        reno_cost += EXTENSION_ALLOWANCE
    gain = comp - prop.price - reno_cost
    prop.enrichment["equity"] = {
        "renovated_comp": comp, "reno_cost_est": reno_cost, "equity_gain": gain,
    }
    ratio = gain / prop.price if prop.price else 0
    pts = max(0, min(cap, round(ratio / 0.25 * cap)))   # 25%+ gain saturates
    return pts, f"Renovated comp ~£{comp:,}. After ~£{reno_cost:,} works, est. £{gain:,} gain."


def score_plot(prop):
    cap = WEIGHTS["plot_size"]
    plot = prop.enrichment.get("plot") or {}
    acres = plot.get("area_acres")
    if acres is None:
        return round(cap * 0.4), "Plot size unknown — flagged for manual check."
    if acres >= 1.0:   pts = cap
    elif acres >= 0.5: pts = round(cap * 0.87)
    elif acres >= 0.4: pts = round(cap * 0.80)
    elif acres >= 0.25: pts = round(cap * 0.67)
    elif acres >= 0.15: pts = round(cap * 0.47)
    else: pts = round(cap * 0.30)
    src = plot.get("source", "estimate")
    return pts, f"Est. {acres:.2f} acre ({src})."


def score_structural(prop):
    cap = WEIGHTS["structural"]
    hits = _has(prop.description_raw, STRUCTURAL_TERMS)
    pts = round(cap * 0.45)                       # baseline doer-upper potential
    if prop.property_type == "bungalow":
        pts += round(cap * 0.20)                  # extend up / loft
    pts += min(round(cap * 0.35), len(hits) * 4)  # +4 per structural term
    constraints = prop.enrichment.get("constraints") or {}
    if constraints.get("conservation_area") or constraints.get("article_4"):
        pts = round(pts * 0.7)                     # consent harder
    pts = max(0, min(cap, pts))
    note = f"Signals: {', '.join(hits) if hits else 'none in text'}."
    if prop.property_type == "bungalow":
        note = "Bungalow — extend-up case. " + note
    return pts, note


def score_motivation(prop):
    cap = WEIGHTS["motivation"]
    market = prop.enrichment.get("market") or {}
    hits = _has(prop.description_raw, MOTIVATION_TERMS)

    # Reductions and days-on-market: prefer the source's structured fields
    # (Homedata supplies these directly); fall back to accrued price history.
    reductions = market.get("reductions")
    if reductions is None:
        reductions = max(0, len(getattr(prop, "price_history", [])) - 1)
    dom = market.get("dom") or prop.days_on_market
    status = (market.get("status") or "").lower()

    pts = 0
    pts += min(round(cap * 0.4), len(hits) * 5)        # description keywords (scraper route)
    pts += min(round(cap * 0.5), int(reductions) * 5)  # price reductions
    if dom and dom > 90:   pts += round(cap * 0.25)
    elif dom and dom > 60: pts += round(cap * 0.15)
    if status in ("reduced", "withdrawn", "re-listed", "relisted"):
        pts += round(cap * 0.1)
    pts = max(0, min(cap, pts))

    bits = []
    if hits: bits.append("'" + "', '".join(hits) + "'")
    if reductions: bits.append(f"{reductions} reduction(s)")
    if dom: bits.append(f"{dom} days on market")
    if status: bits.append(status)
    return pts, "; ".join(bits) if bits else "No motivated-seller signals."


def score_competition(prop):
    """Inverse signal — low buyer interest scores high."""
    cap = WEIGHTS["competition"]
    pts = 0
    media = prop.media or {}
    if (media.get("photo_count") or 0) < 6: pts += 3
    if not media.get("has_floorplan"): pts += 2
    if prop.source.get("portal") in THIN_CHANNELS: pts += 4
    if prop.days_on_market > 75: pts += 2
    if _price_just_above(prop.price): pts += 2
    pts = max(0, min(cap, pts))
    return pts, "Low listing engagement / thin channel — fewer competing buyers."


def score_location(prop):
    cap = WEIGHTS["location"]
    schools = prop.enrichment.get("schools") or []
    station_mi = prop.enrichment.get("station_distance_mi")
    pts = 0
    outstanding = [s for s in schools if (s.get("rating") or "").lower() == "outstanding"]
    if outstanding:
        nearest = min(s["distance_mi"] for s in outstanding)
        pts += round(cap * (0.5 if nearest <= 2 else 0.3))
    if station_mi is not None:
        if station_mi <= 4:   pts += round(cap * 0.5)
        elif station_mi <= 8: pts += round(cap * 0.35)
        else:                 pts += round(cap * 0.2)
    pts = max(0, min(cap, pts))
    note = []
    if outstanding: note.append(f"{outstanding[0]['name']} Outstanding")
    if station_mi is not None: note.append(f"{station_mi:.1f}mi to station")
    return pts, ". ".join(note) if note else "Limited location data."


def _price_just_above(price: int) -> bool:
    """True if price sits just above a common search ceiling (within 2%)."""
    for threshold in (500_000, 600_000, 650_000, 700_000, 750_000, 800_000):
        if threshold < price <= threshold * 1.02:
            return True
    return False


# ---- orchestration --------------------------------------------------------

SIGNAL_FNS = {
    "equity_residual": score_equity,
    "plot_size": score_plot,
    "structural": score_structural,
    "motivation": score_motivation,
    "competition": score_competition,
    "location": score_location,
}


def score(prop):
    """Run all signals, set prop.signals / score / low_comp / scored_at."""
    breakdown, total = {}, 0
    for name, fn in SIGNAL_FNS.items():
        pts, note = fn(prop)
        breakdown[name] = {"score": pts, "max": WEIGHTS[name], "note": note}
        total += pts
    prop.signals = breakdown
    prop.score = round(total / MAX_TOTAL * 100)
    comp_pts = breakdown["competition"]["score"]
    prop.low_comp = (
        comp_pts >= LOW_COMP_THRESHOLD
        or prop.source.get("portal") in THIN_CHANNELS
    )
    prop.scored_at = datetime.now(timezone.utc).isoformat()
    return prop
