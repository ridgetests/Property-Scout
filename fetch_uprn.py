#!/usr/bin/env python3
"""
fetch_uprn.py - download OS Open UPRN (open government data) and build
uprn_coords.json.gz. Run automatically by the "Build data files" button
(.github/workflows/build-data.yml) - you do not run this by hand.

OS Open UPRN is open data, so no account is normally needed. If OS ever requires
a key for the download, set an OS_API_KEY repository secret (free from the OS Data
Hub) and it will be used automatically.
"""
import glob
import json
import os
import subprocess
import sys
import urllib.request
import zipfile

API = "https://api.os.uk/downloads/v1/products/OpenUPRN/downloads?format=CSV&area=GB"


def main():
    key = os.environ.get("OS_API_KEY", "").strip()
    url_api = API + ("&key=" + key if key else "")
    print("Looking up the OS Open UPRN download link...")
    try:
        with urllib.request.urlopen(url_api, timeout=60) as r:
            items = json.load(r)
    except Exception as e:
        print(f"Could not reach the OS download service: {e}")
        sys.exit(1)

    url = None
    for it in (items if isinstance(items, list) else [items]):
        if isinstance(it, dict) and str(it.get("format", "")).upper() == "CSV" and it.get("url"):
            url = it["url"]
            break
    if not url:
        print("No CSV download offered. Service replied:", str(items)[:500])
        print("If this persists, add a free OS_API_KEY secret (OS Data Hub) and re-run.")
        sys.exit(1)

    print("Downloading the UPRN data (a large file - this can take a couple of minutes)...")
    urllib.request.urlretrieve(url, "uprn.zip")
    with zipfile.ZipFile("uprn.zip") as z:
        z.extractall("uprn_data")
    csvs = (glob.glob("uprn_data/**/osopenuprn*.csv", recursive=True)
            or glob.glob("uprn_data/**/*.csv", recursive=True))
    if not csvs:
        print("Downloaded the file but found no CSV inside it.")
        sys.exit(1)

    print(f"Building uprn_coords.json.gz from {csvs[0]} ...")
    subprocess.check_call([sys.executable, "make_uprn.py", csvs[0], "uprn_coords.json.gz"])
    print("Done: uprn_coords.json.gz")


if __name__ == "__main__":
    main()
