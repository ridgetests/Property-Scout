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
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://epc.opendatacommunities.org/api/v1/domestic/search"


def main():
    email = os.environ.get("EPC_OPENDATA_EMAIL", "").strip()
    # Reuse the existing EPC key (the nightly run's EPC_API_KEY) if no dedicated Open Data
    # key is set - it often works on the bulk service too, saving a second sign-up.
    key = os.environ.get("EPC_OPENDATA_KEY", "").strip() or os.environ.get("EPC_API_KEY", "").strip()
    if not email or not key:
        print("Need EPC_OPENDATA_EMAIL (the email your EPC account is registered to) plus an "
              "EPC key (EPC_OPENDATA_KEY, or your existing EPC_API_KEY).")
        sys.exit(1)
    las = [x.strip() for x in os.environ.get("EPC_LAS", "E07000216,E07000085").split(",") if x.strip()]
    out = sys.argv[1] if len(sys.argv) > 1 else "certificates.csv"
    auth = base64.b64encode(f"{email}:{key}".encode()).decode()

    total, wrote_header = 0, False
    with open(out, "w", newline="", encoding="utf-8") as f:
        for la in las:
            after, pages = None, 0
            while True:
                q = {"local-authority": la, "size": "5000"}
                if after:
                    q["search-after"] = after
                req = urllib.request.Request(
                    BASE + "?" + urllib.parse.urlencode(q),
                    headers={"Authorization": "Basic " + auth, "Accept": "text/csv"})
                try:
                    with urllib.request.urlopen(req, timeout=120) as r:
                        body = r.read().decode("utf-8-sig")
                        after = r.headers.get("X-Next-Search-After")
                except Exception as e:
                    print(f"EPC fetch failed for {la}: {e}")
                    print("Check the EPC_OPENDATA_EMAIL / EPC_OPENDATA_KEY secrets are correct.")
                    sys.exit(1)
                lines = [ln for ln in body.splitlines() if ln.strip()]
                if not lines:
                    break
                if wrote_header:
                    lines = lines[1:]          # keep the header only once, from the first page
                else:
                    wrote_header = True
                if lines:
                    f.write("\n".join(lines) + "\n")
                    total += len(lines)
                pages += 1
                if not after:
                    break
                time.sleep(0.3)                # gentle on the service
            print(f"  {la}: {pages} page(s)")
    print(f"wrote ~{total} EPC rows -> {out}")


if __name__ == "__main__":
    main()
