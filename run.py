#!/usr/bin/env python3
"""
PropertyScout pipeline runner.

    fetch (sources) -> normalise -> enrich -> score -> flag -> store -> publish

Run locally:   python run.py
Runs daily in CI via .github/workflows/scout.yml
"""
from __future__ import annotations

from config import SEARCH, ADAPTERS
from adapters.base import load_adapters
from pipeline import store, normalise, score as scorer, flags, publish


def main() -> None:
    print("PropertyScout run starting")
    sources = load_adapters(ADAPTERS["sources"])
    enrichers = load_adapters(ADAPTERS["enrichment"])
    conn = store.connect()

    seen = 0
    for source in sources:
        print(f"- fetching: {source.name} ({source.cost})")
        for raw in source.fetch(SEARCH):
            prop = normalise.normalise(raw)

            for enricher in enrichers:
                try:
                    prop.enrichment.update(enricher.enrich(prop))
                    if "comps" in prop.enrichment:
                        prop.comps = prop.enrichment.pop("comps")
                except Exception as e:
                    print(f"    ! {enricher.name} failed for {prop.id}: {e}")

            # price_history is needed by the motivation signal; pull what we have
            prop.price_history = store.connect().execute(
                "SELECT date, price FROM price_history WHERE id=?", (prop.id,)
            ).fetchall()

            scorer.score(prop)
            prop.flags = flags.detect(prop)
            store.upsert(conn, prop)
            seen += 1
            print(f"    scored {prop.address[:40]:40} -> {prop.score}"
                  f"  (£{prop.enrichment.get('equity', {}).get('equity_gain', 0):,} equity)")

    properties = store.all_live(conn)
    publish.publish(properties)
    conn.close()
    print(f"done — {seen} processed, {len(properties)} live")


if __name__ == "__main__":
    main()
