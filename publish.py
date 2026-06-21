"""
Writes the static properties.json the frontend reads. No server involved —
the file is committed to the repo and served by GitHub Pages.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

OUT_PATH = Path(__file__).parent.parent / "docs" / "properties.json"


def publish(properties: list[dict]) -> None:
    OUT_PATH.parent.mkdir(exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(properties),
        "properties": properties,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"  published {len(properties)} properties -> {OUT_PATH}")
