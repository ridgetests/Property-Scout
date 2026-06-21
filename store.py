"""
SQLite persistence. Upserts canonical records, and captures price changes
and days-on-market automatically across runs.
"""
from __future__ import annotations
import sqlite3
import json
from datetime import date
from pathlib import Path
from pipeline.models import Property

DB_PATH = Path(__file__).parent.parent / "data" / "scout.db"
SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def upsert(conn: sqlite3.Connection, prop: Property) -> None:
    """Insert or update. Tracks first_seen, days_on_market and price changes."""
    today = date.today().isoformat()
    existing = conn.execute(
        "SELECT first_seen, price, relisted_count FROM properties WHERE id=?",
        (prop.id,),
    ).fetchone()

    if existing:
        prop.first_seen = existing["first_seen"]
        if existing["price"] != prop.price:
            conn.execute(
                "INSERT INTO price_history (id, date, price) VALUES (?,?,?)",
                (prop.id, today, prop.price),
            )
    else:
        prop.first_seen = today
        conn.execute(
            "INSERT INTO price_history (id, date, price) VALUES (?,?,?)",
            (prop.id, today, prop.price),
        )

    prop.last_seen = today
    first = date.fromisoformat(prop.first_seen)
    prop.days_on_market = (date.today() - first).days

    conn.execute(
        """
        INSERT INTO properties (
            id, address, postcode, lat, lng, property_type, beds, price,
            first_seen, last_seen, days_on_market, status, relisted_count,
            source_portal, source_listing_id, source_url, source_agent,
            photo_count, has_floorplan, thumb_url, description_raw,
            enrichment_json, comps_json, signals_json, flags_json,
            score, low_comp, scored_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            price=excluded.price, last_seen=excluded.last_seen,
            days_on_market=excluded.days_on_market, status=excluded.status,
            photo_count=excluded.photo_count, has_floorplan=excluded.has_floorplan,
            description_raw=excluded.description_raw,
            enrichment_json=excluded.enrichment_json, comps_json=excluded.comps_json,
            signals_json=excluded.signals_json, flags_json=excluded.flags_json,
            score=excluded.score, low_comp=excluded.low_comp,
            scored_at=excluded.scored_at
        """,
        (
            prop.id, prop.address, prop.postcode, prop.lat, prop.lng,
            prop.property_type, prop.beds, prop.price, prop.first_seen,
            prop.last_seen, prop.days_on_market, prop.status, prop.relisted_count,
            prop.source.get("portal"), prop.source.get("listing_id"),
            prop.source.get("url"), prop.source.get("agent"),
            prop.media.get("photo_count"), int(prop.media.get("has_floorplan", False)),
            prop.media.get("thumb_url"), prop.description_raw,
            json.dumps(prop.enrichment), json.dumps(prop.comps),
            json.dumps(prop.signals), json.dumps(prop.flags),
            prop.score, int(prop.low_comp), prop.scored_at,
        ),
    )
    conn.commit()


def all_live(conn: sqlite3.Connection) -> list[dict]:
    """Return every live property as a publish-ready dict."""
    rows = conn.execute(
        "SELECT * FROM properties WHERE status='live' ORDER BY score DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("enrichment_json", "comps_json", "signals_json", "flags_json"):
            d[k.replace("_json", "")] = json.loads(d.pop(k) or "null")
        d["has_floorplan"] = bool(d["has_floorplan"])
        d["low_comp"] = bool(d["low_comp"])
        ph = conn.execute(
            "SELECT date, price FROM price_history WHERE id=? ORDER BY date", (r["id"],)
        ).fetchall()
        d["price_history"] = [dict(p) for p in ph]
        out.append(d)
    return out
