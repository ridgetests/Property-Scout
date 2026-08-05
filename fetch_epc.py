#!/usr/bin/env python3
"""
fetch_epc.py - download EPC certificates for the target councils from the free
EPC Open Data service and write certificates.csv. Run automatically by the
"Build data files" button (.github/workflows/build-data.yml) - not by hand.

Needs a free EPC Open Data account (https://epc.opendatacommunities.org/). After
registering you get an email + an API key; store them as repository secrets
EPC_OPENDATA_EMAIL and EPC_OPENDATA_KEY. Councils default to Waverley
(E07000216) and East Hampshire (E07000085); override with the EPC_LAS secret
(comma-separated ONS local-authority codes) if your search area changes.
"""
import base64
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://epc.opendatacommunities.org/api/v1/domestic/search"


def _mask(s):
    return (s[:3] + "…" + s[-2:]) if len(s) > 6 else "(set)"


def main():
    email = os.environ.get("EPC_OPENDATA_EMAIL", "").strip()
    dedicated = os.environ.get("EPC_OPENDATA_KEY", "").strip()
    key = dedicated or os.environ.get("EPC_API_KEY", "").strip()
    if not email or not key:
        print("Need EPC_OPENDATA_EMAIL (your EPC account email) plus an EPC key "
              "(EPC_OPENDATA_KEY, or the existing EPC_API_KEY).")
        sys.exit(1)
    # NB: a workflow env from an UNSET secret arrives as "" (not absent), so os.environ.get's
    # default never fires - use `or` so an empty EPC_LAS still falls back to the defaults.
    las_src = (os.environ.get("EPC_LAS") or "").strip() or "E07000216,E07000085"
    las = [x.strip() for x in las_src.split(",") if x.strip()]
    out = sys.argv[1] if len(sys.argv) > 1 else "certificates.csv"
    print(f"Using email '{email}' and the "
          f"{'dedicated Open Data key' if dedicated else 'existing EPC_API_KEY (fallback)'} "
          f"({_mask(key)}). Councils: {', '.join(las)}")

    auth = base64.b64encode(f"{email}:{key}".encode()).decode()
    headers = {"Authorization": "Basic " + auth,
               "Accept": "application/json",
               "User-Agent": "PropertyScout/1.0 (personal use)"}

    total, writer, diag = 0, None, True
    csv_f = open(out, "w", newline="", encoding="utf-8")
    try:
        for la in las:
            after, pages, la_rows = None, 0, 0
            while True:
                q = {"local-authority": la, "size": "5000"}
                if after:
                    q["search-after"] = after
                url = BASE + "?" + urllib.parse.urlencode(q)
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                        status = r.status
                        ctype = r.headers.get("Content-Type", "")
                        after = r.headers.get("X-Next-Search-After")
                        body = r.read().decode("utf-8-sig", "replace")
                except urllib.error.HTTPError as e:
                    detail = ""
                    try:
                        detail = e.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass
                    print(f"  {la}: HTTP {e.code} {e.reason}. {detail}")
                    if e.code in (401, 403):
                        print("  -> The bulk service rejected the key/email. Make sure EPC_OPENDATA_KEY")
                        print("     is a key from https://epc.opendatacommunities.org/ (a separate")
                        print("     account from the one-house EPC service), registered to this email.")
                    sys.exit(1)
                except Exception as e:
                    print(f"  {la}: request failed: {e}")
                    sys.exit(1)

                if diag:                       # print what the FIRST response looked like
                    diag = False
                    print(f"  first response: HTTP {status}, Content-Type '{ctype}', "
                          f"next-page {'yes' if after else 'no'}, body {len(body)} chars")
                    print(f"  body starts: {body[:200]!r}")

                try:
                    data = json.loads(body) if body.strip() else {}
                except Exception:
                    print(f"  {la}: could not read the reply as data. It started: {body[:200]!r}")
                    sys.exit(1)
                rows = data.get("rows") if isinstance(data, dict) else data
                if not rows:
                    break
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if writer is None:
                        writer = csv.DictWriter(csv_f, fieldnames=list(row.keys()),
                                                extrasaction="ignore", restval="")
                        writer.writeheader()
                    writer.writerow(row)
                    total += 1
                    la_rows += 1
                pages += 1
                if not after:
                    break
                time.sleep(0.3)               # gentle on the service
            print(f"  {la}: {pages} page(s), {la_rows} rows")
    finally:
        csv_f.close()

    if total == 0:
        print("No EPC rows were returned. Look at the 'first response' line above:")
        print("  - HTTP 401/403  => wrong key for the bulk service (get an Open Data key).")
        print("  - HTTP 200, body 0 chars => key not accepted, or the council code is wrong.")
        print("  - body looks like an error page => the request needs adjusting; send me this log.")
        sys.exit(1)
    print(f"wrote {total} EPC rows -> {out}")


if __name__ == "__main__":
    main()
