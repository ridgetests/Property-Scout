#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_agents.py  --  PropertyScout local estate-agent listings fetcher.

WHAT IT DOES
------------
Politely aggregates on-market listings from local estate agents' OWN websites
(NOT Rightmove/Zoopla) and returns them in the shape PropertyScout expects:

    {"address": ..., "postcode": ..., "price": ..., "type": ..., "link": ...}

plus a few extra fields the engine can use or ignore (source, beds, status,
price_reduced, previous_price, lastmod, first_seen).

HOW IT WORKS (and why it is safe)
---------------------------------
For each agent it:
  1. Reads robots.txt and OBEYS it (disallowed paths are skipped; a robots
     Crawl-delay is honoured, with a 10s floor).
  2. Finds the agent's property URLs from their XML sitemap (or a listing page),
     handling sitemap-index files and .gz sitemaps.
  3. Diffs those URLs against a saved snapshot (agents_state.json) so it only
     fetches NEW or CHANGED pages -- after the first run that is a handful a day,
     not hundreds. A per-run fetch budget caps even the first run.
  4. Extracts the fields from each detail page: JSON-LD (schema.org) first,
     then OpenGraph/meta tags, then plain-text regex -- so it degrades
     gracefully across different agent website software.
  5. A circuit-breaker parks a whole domain for the rest of the run on the first
     401/403/429 (protects your access -- a throttle heals, a ban is fatal).

The full set of currently-listed properties is emitted every run (read from the
saved snapshot), so downstream scoring always sees the whole book even on days
when nothing new was fetched.

DEPENDENCIES
------------
Standard library + `requests` only (already a PropertyScout dependency).
`beautifulsoup4` is used automatically IF present, but is NOT required.

USE FROM run.py
---------------
    from fetch_agents import fetch_agent_listings
    agent_leads = fetch_agent_listings()          # list of dicts (PropertyScout shape)
    leads.extend(agent_leads)                       # merge into the gather step

Or run standalone to test:
    python3 fetch_agents.py            # full run, writes agent_listings.json
    python3 fetch_agents.py --selftest # one permissive agent, 1 page, to smoke-test
    python3 fetch_agents.py --list     # just print the agent registry

DEPLOY (phone / GitHub web UI)
------------------------------
  * Upload this file to the repo root.
  * Ensure the nightly workflow COMMITS agents_state.json back to the repo
    (same commit step that saves docs/properties.json) so the diff/history
    survives between runs. If it cannot commit, the tool still works -- it just
    re-scans more each run.
  * No new pip packages required.

FIELD-NAME NOTE
---------------
Output keys are address/postcode/price/type/link (+extras). If run.py uses
different keys (e.g. "url" instead of "link", or "property_type"), adjust the
NORMALISED_KEYS mapping near the bottom -- it is a one-line change.
"""

from __future__ import annotations

import argparse
import gzip
import html
import io
import json
import os
import re
import sys
import time
from urllib import robotparser
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
import xml.etree.ElementTree as ET

import requests

# Optional, nicer HTML parsing if available. Not required.
try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAVE_BS4 = True
except Exception:  # pragma: no cover
    _HAVE_BS4 = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Be identifiable and honest. Put a real contact in here so an agent can reach
# you rather than block you.
USER_AGENT = os.environ.get(
    "PROPERTYSCOUT_UA",
    "PropertyScoutBot/1.0 (+personal property research; contact: heystevenridgeway@gmail.com)",
)

DEFAULT_CRAWL_DELAY = float(os.environ.get("AGENTS_CRAWL_DELAY", "10"))   # seconds, floor
REQUEST_TIMEOUT = float(os.environ.get("AGENTS_TIMEOUT", "30"))          # seconds (retried x3 w/ backoff)
MAX_DETAIL_FETCHES_PER_AGENT = int(os.environ.get("AGENTS_MAX_FETCHES", "40"))
MAX_URLS_PER_AGENT = int(os.environ.get("AGENTS_MAX_URLS", "600"))        # sitemap safety cap
MAX_LISTING_PAGES = int(os.environ.get("AGENTS_MAX_LISTING_PAGES", "25"))  # listing-mode page cap

STATE_FILE = os.environ.get("AGENTS_STATE_FILE", "agents_state.json")
OUTPUT_FILE = os.environ.get("AGENTS_OUTPUT_FILE", "agent_listings.json")

# HTTP statuses that mean "back off this whole domain for the rest of the run".
_BREAKER_STATUSES = {401, 403, 429}

# UK postcode (outward+inward). Case-insensitive; we search original-case text.
_POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.IGNORECASE
)

# Price forms: "£850,000", "Guide Price £1,250,000", "£1.25m", "£850k",
# "Offers in excess of £675,000". We normalise to an int of pounds.
_PRICE_COMMA_RE = re.compile(r"£\s*(\d{1,3}(?:,\d{3})+)")
_PRICE_SHORT_RE = re.compile(r"£\s*(\d+(?:\.\d+)?)\s*([mMkK])")
_PRICE_PLAIN_RE = re.compile(r"£\s*(\d{5,7})(?!\d)")

# Rough sanity band for a residential/land price (pounds). Outside this we treat
# the number as noise (e.g. a service charge or square-footage).
_PRICE_MIN = 40_000
_PRICE_MAX = 15_000_000

# Property-type keywords -> canonical label. Order matters (first hit wins).
_TYPE_KEYWORDS = [
    ("barn conversion", "Barn Conversion"),
    ("barn", "Barn"),
    ("detached bungalow", "Detached Bungalow"),
    ("semi-detached bungalow", "Semi-Detached Bungalow"),
    ("bungalow", "Bungalow"),
    ("building plot", "Land/Plot"),
    ("development", "Development"),
    ("plot", "Land/Plot"),
    ("land", "Land/Plot"),
    ("smallholding", "Smallholding"),
    ("farmhouse", "Farmhouse"),
    ("farm", "Farm"),
    ("cottage", "Cottage"),
    ("country house", "Country House"),
    ("town house", "Town House"),
    ("townhouse", "Town House"),
    ("end of terrace", "End Terrace"),
    ("end terrace", "End Terrace"),
    ("mid terrace", "Terraced"),
    ("terraced", "Terraced"),
    ("terrace", "Terraced"),
    ("semi-detached", "Semi-Detached"),
    ("semi detached", "Semi-Detached"),
    ("detached", "Detached"),
    ("maisonette", "Maisonette"),
    ("penthouse", "Penthouse"),
    ("apartment", "Apartment/Flat"),
    ("flat", "Apartment/Flat"),
    ("studio", "Apartment/Flat"),
]

# schema.org @types that indicate a property/offer node.
_LD_PROPERTY_TYPES = {
    "product", "realestatelisting", "residence", "singlefamilyresidence",
    "house", "apartment", "accommodation", "place", "offer", "listing",
}


# ---------------------------------------------------------------------------
# Agent registry
#
# Each agent: key, name, and how to find its property URLs.
#   sitemaps      : explicit sitemap URL(s). If omitted, we discover them from
#                   robots.txt.
#   listing_pages : (optional) paginated search pages to scrape detail links
#                   from -- used when there is no clean property sitemap
#                   (e.g. a national franchise whose global sitemap is huge).
#   detail_patterns: regexes; a URL is treated as a property detail page only if
#                    one matches. This filters out blog/area-guide URLs.
#
# Excluded on purpose (see the research report §A6):
#   * Bourne          -- whole site 401s to bots; no compliant automated route.
#   * Masella Coupe   -- portal-only, no own-site listings.
#   * Keats Fearn     -- JavaScript single-page app; needs a headless browser.
#   * Peter Leete     -- structure unverified (paths 404'd); enable once checked.
# ---------------------------------------------------------------------------

AGENTS = [
    {
        "key": "andrew_lodge",
        "name": "Andrew Lodge",
        "sitemaps": ["https://andrewlodge.net/tpj_properties-sitemap.xml"],
        "detail_patterns": [r"/properties/sale/"],
        "enabled": True,
        "notes": "Confirmed clean: robots allows, Crawl-delay 10s, server-rendered. Best single source.",
    },
    {
        "key": "trueman_grundy",
        "name": "Trueman & Grundy",
        "sitemaps": ["https://www.truemanandgrundy.co.uk/wppf_property-sitemap.xml"],
        "discover": True,  # fall back to robots.txt discovery if the explicit one 404s
        "detail_patterns": [r"/property/"],
        "enabled": True,
    },
    {
        "key": "warren_powell_richards",
        "name": "Warren Powell Richards",
        "home": "https://www.wpr.co.uk",
        "sitemaps": ["https://www.wpr.co.uk/sitemap_index.xml"],
        "discover": True,  # robots.txt is the authoritative sitemap source; guess is a fallback
        "detail_patterns": [r"/properties/sale/"],
        "enabled": True,
    },
    {
        "key": "curchods",
        "name": "Curchods",
        "sitemaps": ["https://www.curchods.com/sitemap.xml"],
        "discover": True,
        "detail_patterns": [r"/display/", r"/property/"],
        "enabled": True,
    },
    {
        "key": "charters",
        "name": "Charters",
        "home": "https://www.chartersestateagents.co.uk",
        "sitemaps": ["https://www.chartersestateagents.co.uk/sitemap_index.xml"],
        "discover": True,
        "detail_patterns": [r"/property-for-sale/"],
        "enabled": True,
        "notes": "Starberry/Gatsby -- likely emits JSON-LD. Verify view-source once.",
    },
    {
        "key": "mackenzie_smith",
        "name": "Mackenzie Smith",
        "home": "https://www.mackenziesmith.co.uk",
        "sitemaps": ["https://www.mackenziesmith.co.uk/sitemap_index.xml"],
        "discover": True,
        "detail_patterns": [r"/property/"],
        "enabled": True,
    },
    {
        "key": "seymours_godalming",
        "name": "Seymours (Godalming)",
        "home": "https://www.seymours-estates.co.uk",
        # Split site: WordPress marketing pages vs a /branches/.../sales/ property
        # feed. The WP sitemap may not carry detail pages, so also crawl the branch
        # listing pages directly. Detail-URL shape is unconfirmed -- verify from a run.
        "listing_pages": ["https://www.seymours-estates.co.uk/branches/godalming-sales/sales"],
        "discover": True,
        "detail_patterns": [r"/property/", r"/branches/[^/]+/sales/[^/]", r"/offices/[^/]+/sales/[^/]"],
        "enabled": True,
        "notes": "LOW confidence: property subsystem separate from the WP site; may need "
                 "detail-pattern tuning after the first run's log is seen.",
    },
    {
        "key": "clarke_gammon",
        "name": "Clarke Gammon",
        "sitemaps": ["https://www.clarkegammon.co.uk/property-sitemap.xml"],
        "discover": True,
        "detail_patterns": [r"/property/"],
        "enabled": True,
        "notes": "Also the best land/plot source in the patch.",
    },
    {
        "key": "homes_ea",
        "name": "Homes Estate Agents",
        "home": "https://homesea.co.uk",
        "sitemaps": ["https://homesea.co.uk/sitemap_index.xml"],
        "discover": True,
        "detail_patterns": [r"/property-for-sale/"],
        "enabled": True,
        "notes": "Covers the GU35 Hampshire side (Bordon/Whitehill/Liphook).",
    },
    {
        "key": "winkworth_farnham",
        "name": "Winkworth (Farnham)",
        "home": "https://www.winkworth.co.uk",
        # Global Winkworth sitemap is national/huge, so crawl the branch listing
        # pages instead and pull detail links from them. (Branch path is /branches/;
        # the old /estate-agents/ path returned nothing -- that was the mute cause.)
        "listing_pages": [
            "https://www.winkworth.co.uk/branches/farnham/properties-for-sale",
        ],
        "detail_patterns": [r"/properties/sales?/"],
        "enabled": True,
        "notes": "Branch listing-page mode; pagination is automatic (follows next-page link, "
                 "falls back to ?page=N). Large national site -- listing pages may be "
                 "JS-rendered; if so, an XML property sitemap is the fallback to add.",
    },
    {
        "key": "grantley",
        "name": "Grantley",
        "home": "https://grantley.co.uk",
        "sitemaps": ["https://grantley.co.uk/property-sitemap.xml"],  # guess; robots.txt is authoritative
        "discover": True,
        "listing_pages": ["https://grantley.co.uk/sales/"],
        "detail_patterns": [r"/property/", r"/sales/[^/]+/[^/]"],
        "enabled": True,
        "notes": "Local independent (Surrey/W.Sussex/Hants); covers Farnham GU9-GU10. Sitemap "
                 "guessed -- verify detail_patterns from the first diagnostic.",
    },
    {
        "key": "hamptons_farnham",
        "name": "Hamptons (Farnham)",
        "home": "https://www.hamptons.co.uk",
        # National franchise -> global sitemap is huge, so crawl the Farnham branch pages.
        "listing_pages": ["https://www.hamptons.co.uk/branches/farnham/sales"],
        "detail_patterns": [r"/property", r"/branches/farnham/sales/[^/?]+$"],
        "enabled": True,
        "notes": "~127 Farnham listings; branch pages are server-rendered (page-N pagination). "
                 "If the detail HTML is JS-only this yields little -- confirm from diagnostic.",
    },
    {
        "key": "strutt_parker_farnham",
        "name": "Strutt & Parker (Farnham)",
        "home": "https://www.struttandparker.com",
        "listing_pages": ["https://www.struttandparker.com/properties/residential/for-sale/surrey/farnham"],
        "detail_patterns": [r"/property/", r"/properties/[^/]+/[^/]"],
        "enabled": True,
        "notes": "High-end GU10 stock (Tilford/Lower Bourne/Churt). Franchise -- may be "
                 "JS-rendered; verify from diagnostic.",
    },
    {
        "key": "purplebricks",
        "name": "Purplebricks (online / ex-Strike)",
        "home": "https://www.purplebricks.co.uk",
        # Online/DIY agent -- the "not a high-street agency" angle. Strike merged into it.
        "listing_pages": ["https://www.purplebricks.co.uk/search/property-for-sale/surrey/farnham"],
        "detail_patterns": [r"/property-for-sale/"],
        "discover": True,
        "enabled": True,
        "notes": "Few local listings (~4) and a JS-heavy site, so may yield little. Most "
                 "Purplebricks/Strike stock also reaches us via Homedata (it's on the "
                 "portals). Experimental; verify from diagnostic.",
    },
    {
        "key": "henry_adams",
        "name": "Henry Adams (Haslemere)",
        "discover": True,
        "detail_patterns": [r"/propert"],
        "enabled": False,  # enable once URL structure confirmed
    },
    {
        "key": "peter_leete",
        "name": "Peter Leete & Partners",
        "discover": True,
        "detail_patterns": [r"/propert"],
        "enabled": False,  # structure unverified -- confirm before enabling
    },
]


# ---------------------------------------------------------------------------
# HTTP session with polite defaults + a per-domain circuit-breaker
# ---------------------------------------------------------------------------

class Fetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        })
        self._dead_domains: set[str] = set()      # circuit-breaker
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._last_request_at: dict[str, float] = {}
        self._log: list[str] = []

    # -- logging -----------------------------------------------------------
    def log(self, msg: str):
        line = f"[fetch_agents] {msg}"
        self._log.append(line)
        print(line, file=sys.stderr)

    # -- circuit-breaker ---------------------------------------------------
    def _domain(self, url: str) -> str:
        return urlparse(url).netloc.lower()

    def is_dead(self, url: str) -> bool:
        return self._domain(url) in self._dead_domains

    def kill(self, url: str, why: str):
        dom = self._domain(url)
        if dom not in self._dead_domains:
            self._dead_domains.add(dom)
            self.log(f"circuit-breaker tripped for {dom}: {why}")

    # -- robots.txt --------------------------------------------------------
    def robots(self, url: str) -> robotparser.RobotFileParser:
        dom = self._domain(url)
        if dom in self._robots:
            return self._robots[dom]
        rp = robotparser.RobotFileParser()
        robots_url = f"{urlparse(url).scheme}://{dom}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=REQUEST_TIMEOUT)
            if resp.status_code in _BREAKER_STATUSES:
                self.kill(url, f"robots.txt returned {resp.status_code}")
                rp.disallow_all = True  # be conservative
            elif resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp.parse([])  # no robots -> allow
        except requests.RequestException as e:
            self.log(f"robots.txt fetch failed for {dom} ({e}); assuming allow")
            rp.parse([])
        self._robots[dom] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self.robots(url)
        try:
            return rp.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def crawl_delay(self, url: str) -> float:
        rp = self.robots(url)
        try:
            d = rp.crawl_delay(USER_AGENT)
        except Exception:
            d = None
        return max(DEFAULT_CRAWL_DELAY, float(d) if d else 0.0)

    def sitemaps_from_robots(self, url: str) -> list[str]:
        rp = self.robots(url)
        try:
            sm = rp.site_maps()
            return list(sm) if sm else []
        except Exception:
            return []

    # -- polite GET --------------------------------------------------------
    def get(self, url: str, *, respect_delay: bool = True):
        """Return requests.Response or None. Honours robots, delay, breaker."""
        if self.is_dead(url):
            return None
        if not self.allowed(url):
            self.log(f"robots disallows, skipping: {url}")
            return None
        dom = self._domain(url)
        if respect_delay:
            delay = self.crawl_delay(url)
            last = self._last_request_at.get(dom)
            if last is not None:
                wait = delay - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
        # A slow-but-alive server (Andrew Lodge times out intermittently) or a dropped
        # connection (Strutt & Parker: RemoteDisconnected) is transient -- retry a couple
        # of times with backoff and a longer timeout before giving up.
        resp = None
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT * (1 + attempt),
                                        allow_redirects=True)
                break
            except (requests.Timeout, requests.ConnectionError) as e:
                self._last_request_at[dom] = time.monotonic()
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))            # 3s, 6s -- polite, brief
                    continue
                self.log(f"GET failed {url} after {attempt + 1} tries: {e}")
                return None
            except requests.RequestException as e:
                self.log(f"GET failed {url}: {e}")
                self._last_request_at[dom] = time.monotonic()
                return None
        self._last_request_at[dom] = time.monotonic()
        if resp.status_code in _BREAKER_STATUSES:
            self.kill(url, f"GET {url} -> {resp.status_code}")
            return None
        if resp.status_code != 200:
            self.log(f"GET {url} -> {resp.status_code}")
            return None
        return resp


# ---------------------------------------------------------------------------
# Sitemap handling
# ---------------------------------------------------------------------------

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _read_xml_bytes(content: bytes) -> ET.Element | None:
    # Handle gzip (either by content or .gz naming).
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except Exception:
            return None
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        # Some sitemaps have a stray BOM / leading whitespace.
        try:
            return ET.fromstring(content.strip())
        except Exception:
            return None


def parse_sitemap(fetcher: Fetcher, url: str, depth: int = 0) -> list[dict]:
    """Return list of {loc, lastmod} entries, recursing into sitemap indexes."""
    if depth > 3:
        return []
    resp = fetcher.get(url)
    if resp is None:
        return []
    root = _read_xml_bytes(resp.content)
    if root is None:
        return []
    tag = _strip_ns(root.tag).lower()
    entries: list[dict] = []
    if tag == "sitemapindex":
        for sm in root:
            loc = None
            for child in sm:
                if _strip_ns(child.tag).lower() == "loc" and child.text:
                    loc = child.text.strip()
            if loc:
                entries.extend(parse_sitemap(fetcher, loc, depth + 1))
    else:  # urlset (or unknown -> try to read <url><loc>)
        for u in root:
            loc, lastmod = None, None
            for child in u:
                t = _strip_ns(child.tag).lower()
                if t == "loc" and child.text:
                    loc = child.text.strip()
                elif t == "lastmod" and child.text:
                    lastmod = child.text.strip()
            if loc:
                entries.append({"loc": loc, "lastmod": lastmod})
    return entries


def _detail_links_in_html(html: str, base: str) -> set[str]:
    out = set()
    for h in set(re.findall(r'href=["\']([^"\']+)["\']', html)):
        out.add(urljoin(base, h.split("#")[0]))
    return out


_PAGE_PARAM_KEYS = ("page", "pn", "pg", "p")


def _find_next_link(html: str, base: str) -> str | None:
    """Find a rel=next pagination link (SEO-standard on most property sites)."""
    for pat in (
        r'<link[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']',
        r'<a[^>]+rel=["\']next["\'][^>]*href=["\']([^"\']+)["\']',
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\']next["\']',
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return urljoin(base, m.group(1).split("#")[0])
    return None


def _guess_next_page(url: str) -> str | None:
    """Fallback when there's no rel=next: increment (or add) a page parameter."""
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    for k in _PAGE_PARAM_KEYS:
        if k in q:
            try:
                q[k] = str(int(q[k]) + 1)
            except ValueError:
                return None
            return urlunparse(parts._replace(query=urlencode(q)))
    q["page"] = "2"  # no page param yet -> try page 2
    return urlunparse(parts._replace(query=urlencode(q)))


def crawl_listing_pages(fetcher: Fetcher, start_url: str, base: str,
                        detail_patterns: list[str]) -> list[dict]:
    """Walk a paginated listing/search area, following the 'next page' link.

    Uses rel=next where present; otherwise increments a ?page= parameter. Stops
    at MAX_LISTING_PAGES, when two pages in a row add no new detail links, or
    when the domain's circuit-breaker trips.
    """
    collected: dict[str, dict] = {}
    visited: set[str] = set()
    queue: list[str] = [start_url]
    pages = 0
    no_new_streak = 0

    while queue and pages < MAX_LISTING_PAGES:
        url = queue.pop(0)
        if url in visited or fetcher.is_dead(url):
            continue
        visited.add(url)
        resp = fetcher.get(url)
        pages += 1
        if resp is None:
            break  # 404/blocked/breaker -> stop paginating this area
        html = resp.text

        new = 0
        for absu in _detail_links_in_html(html, base):
            if _looks_like_detail(absu, detail_patterns) and absu not in collected:
                collected[absu] = {"loc": absu, "lastmod": None}
                new += 1
        no_new_streak = no_new_streak + 1 if new == 0 else 0
        if no_new_streak >= 2:
            break

        nxt = _find_next_link(html, base) or _guess_next_page(url)
        if nxt and nxt not in visited and nxt != url:
            queue.append(nxt)

    fetcher.log(f"listing crawl {start_url}: {pages} page(s), {len(collected)} detail links")
    return list(collected.values())


# ---------------------------------------------------------------------------
# Detail-page extraction
# ---------------------------------------------------------------------------

def _iter_ld_nodes(data):
    """Yield dict nodes from JSON-LD, flattening @graph and lists."""
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld_nodes(item)
    elif isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            for item in data["@graph"]:
                yield from _iter_ld_nodes(item)
        yield data


def _ld_type_matches(node: dict) -> bool:
    t = node.get("@type")
    if t is None:
        return False
    types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
    return any(str(x).lower() in _LD_PROPERTY_TYPES for x in types)


def _price_from_number(raw) -> int | None:
    try:
        val = int(round(float(str(raw).replace(",", "").replace("£", "").strip())))
    except (ValueError, TypeError):
        return None
    return val if _PRICE_MIN <= val <= _PRICE_MAX else None


def extract_jsonld(html: str) -> dict:
    """Pull address/postcode/price/type from any JSON-LD property node."""
    out: dict = {}
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    for block in blocks:
        raw = block.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Some sites emit multiple concatenated objects or trailing commas.
            continue
        for node in _iter_ld_nodes(data):
            if not isinstance(node, dict):
                continue
            is_prop = _ld_type_matches(node)
            # address
            addr = node.get("address")
            if addr and "address" not in out:
                if isinstance(addr, dict):
                    parts = [addr.get("streetAddress"), addr.get("addressLocality"),
                             addr.get("addressRegion")]
                    parts = [p for p in parts if p]
                    if parts:
                        out["address"] = ", ".join(parts)
                    pc = addr.get("postalCode")
                    if pc:
                        out["postcode"] = str(pc).strip().upper()
                elif isinstance(addr, str) and addr.strip():
                    out["address"] = addr.strip()
            # name as address fallback
            if is_prop and "address" not in out and node.get("name"):
                out["address"] = str(node["name"]).strip()
            # price (offers may be nested)
            if "price" not in out:
                offers = node.get("offers")
                cand = None
                if isinstance(offers, dict):
                    cand = offers.get("price") or offers.get("lowPrice")
                elif isinstance(offers, list) and offers:
                    first = offers[0]
                    if isinstance(first, dict):
                        cand = first.get("price") or first.get("lowPrice")
                if cand is None:
                    cand = node.get("price")
                p = _price_from_number(cand) if cand is not None else None
                if p:
                    out["price"] = p
            # type
            if is_prop and "type" not in out:
                t = node.get("@type")
                tstr = t if isinstance(t, str) else (t[0] if isinstance(t, list) and t else None)
                # skip container/page @types - they are not dwelling types and only
                # leak as junk labels ("webpage", "realestatelisting", "product").
                if tstr and str(tstr).lower() not in {
                        "product", "offer", "place", "listing", "webpage", "website",
                        "realestatelisting", "organization", "breadcrumblist", "itemlist"}:
                    out["type"] = str(tstr)
            # description (feeds downstream planning-signal detection)
            if "description" not in out and node.get("description"):
                out["description"] = str(node["description"]).strip()
            # main photo (schema.org image: string | list | ImageObject)
            if "image" not in out and node.get("image"):
                img = node["image"]
                if isinstance(img, list) and img:
                    img = img[0]
                if isinstance(img, dict):
                    img = img.get("url") or img.get("contentUrl")
                if isinstance(img, str) and img.strip().startswith("http"):
                    out["image"] = img.strip()
    return out


def extract_meta(html: str) -> dict:
    """OpenGraph / meta fallback."""
    out = {}
    def meta(prop):
        m = re.search(
            r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']+)["\']' % re.escape(prop),
            html, re.IGNORECASE)
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']%s["\']' % re.escape(prop),
                html, re.IGNORECASE)
        return m.group(1).strip() if m else None

    title = meta("og:title")
    desc = meta("og:description")
    price_amt = meta("product:price:amount") or meta("og:price:amount")
    if title:
        out["address"] = re.sub(r"\s+", " ", title)
    if price_amt:
        p = _price_from_number(price_amt)
        if p:
            out["price"] = p
    # title tag fallback for address
    if "address" not in out:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        if m:
            out["address"] = re.sub(r"\s+", " ", m.group(1)).strip()
    if desc:
        out["description"] = desc
    img = meta("og:image") or meta("og:image:secure_url") or meta("twitter:image")
    if img and img.strip().startswith("http"):
        out["image"] = img.strip()
    out["_text_blob"] = " ".join(x for x in (title, desc) if x)
    return out


def _visible_text(page_html: str) -> str:
    if _HAVE_BS4:
        try:
            soup = BeautifulSoup(page_html, "html.parser")
            for s in soup(["script", "style", "noscript"]):
                s.extract()
            return re.sub(r"\s+", " ", soup.get_text(" "))
        except Exception:
            pass
    # stdlib fallback. Crucially, DECODE HTML entities -- otherwise a price
    # written as "&pound;850,000" or "&#163;850,000" keeps its literal entity
    # and the "£" price regex never matches, silently producing a £0 listing.
    txt = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", page_html)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    return re.sub(r"\s+", " ", txt)


# og:title is very often "Property address | Agent Name" (or with a – / — separator).
# Keep only the address half so the agent's name doesn't pollute the address (and the
# postcode/type detectors that read it).
_AGENT_PIPE_RE = re.compile(r"\s*[|–—]\s.*$")
_AGENT_DASH_RE = re.compile(
    r"\s-\s.*\b(estate agents?|property|properties|lettings|sales|homes|& partners|"
    r"residential)\b.*$", re.IGNORECASE)
# an outward code (GU9, GU10, RG29) NOT followed by an inward code
_OUTWARD_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b(?!\s*\d[A-Z]{2})", re.IGNORECASE)


def _strip_agent_suffix(s: str) -> str:
    s = _AGENT_PIPE_RE.sub("", s or "")
    s = _AGENT_DASH_RE.sub("", s)
    return s.strip(" ,|-–—")


def _resolve_postcode(address: str, existing: str, blob: str) -> str:
    """Postcode for a listing, guarding against the agent's OWN office postcode that
    frequently sits in the page footer/schema. Priority:
      1. a FULL postcode written in the address itself (most trustworthy);
      2. a full postcode found elsewhere (JSON-LD, then page text) - but ONLY if its
         outward code matches the address's outward code (else it's likely the office);
      3. the address's outward code alone (correct town, geocoded to its centroid).
    Returns a full or outward-only postcode, or ''."""
    addr = address or ""
    m = _POSTCODE_RE.search(addr)
    if m:
        return f"{m.group(1).upper()} {m.group(2).upper()}"
    ow = _OUTWARD_RE.search(addr)
    addr_outward = ow.group(1).upper() if ow else None
    cand = None
    for src in (existing, blob):
        mm = _POSTCODE_RE.search(src or "")
        if mm:
            cand = f"{mm.group(1).upper()} {mm.group(2).upper()}"
            break
    if cand and (addr_outward is None or cand.split()[0] == addr_outward):
        return cand
    return addr_outward or ""


def parse_price_from_text(text: str) -> int | None:
    for m in _PRICE_COMMA_RE.finditer(text):
        p = _price_from_number(m.group(1))
        if p:
            return p
    for m in _PRICE_SHORT_RE.finditer(text):
        num, unit = m.group(1), m.group(2).lower()
        try:
            val = float(num) * (1_000_000 if unit == "m" else 1_000)
        except ValueError:
            continue
        val = int(round(val))
        if _PRICE_MIN <= val <= _PRICE_MAX:
            return val
    for m in _PRICE_PLAIN_RE.finditer(text):
        p = _price_from_number(m.group(1))
        if p:
            return p
    return None


def parse_postcode_from_text(text: str) -> str | None:
    m = _POSTCODE_RE.search(text)
    if m:
        return f"{m.group(1).upper()} {m.group(2).upper()}"
    return None


def guess_type(text: str) -> str | None:
    low = text.lower()
    for kw, label in _TYPE_KEYWORDS:
        if kw in low:
            return label
    return None


def parse_status(text: str) -> str | None:
    low = text.lower()
    for phrase, label in [
        ("under offer", "Under Offer"),
        ("sold stc", "Sold STC"),
        ("sold subject to contract", "Sold STC"),
        ("sale agreed", "Sale Agreed"),
        ("let agreed", "Let Agreed"),
        ("new instruction", "New"),
        ("coming soon", "Coming Soon"),
    ]:
        if phrase in low:
            return label
    return None


_POA_RE = re.compile(
    r"price\s+on\s+application|price\s+on\s+request|poa\b|guide\s+price\s+t\.?b\.?c|"
    r"price\s+guide\s+t\.?b\.?c|offers\s+invited",
    re.IGNORECASE,
)


def looks_poa(text: str) -> bool:
    """True when the page says the price is on application / not published, so a
    missing price is intentional (POA), not a failed extraction."""
    return bool(_POA_RE.search(text or ""))


def parse_beds(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*(?:bed(?:room)?s?)\b", text, re.IGNORECASE)
    if m:
        try:
            n = int(m.group(1))
            if 0 < n <= 15:
                return n
        except ValueError:
            pass
    return None


def extract_listing(page_html: str, url: str) -> dict:
    """Combine JSON-LD -> meta -> text heuristics into one record."""
    rec: dict = {"link": url}
    ld = extract_jsonld(page_html)
    rec.update({k: v for k, v in ld.items() if v})

    meta = extract_meta(page_html)
    for k in ("address", "price", "image"):
        if not rec.get(k) and meta.get(k):
            rec[k] = meta[k]

    text = _visible_text(page_html)
    # decode entities in the combined blob too: og:title/description come through raw, so
    # a price written "&pound;650,000" in the description would otherwise be missed.
    blob = html.unescape((meta.get("_text_blob", "") + " " + text)).strip()

    # Clean the address first: drop the "| Agent Name" tail and decode entities, so the
    # address (and the postcode/type read from it) isn't polluted by the agent's branding.
    if rec.get("address"):
        rec["address"] = _strip_agent_suffix(html.unescape(rec["address"]))

    if not rec.get("price"):
        p = parse_price_from_text(blob)
        if p:
            rec["price"] = p
    # Postcode: address-first, and never silently adopt the agent's office postcode.
    rec["postcode"] = _resolve_postcode(rec.get("address", ""), rec.get("postcode", ""), blob)
    if not rec.get("type"):
        # match the type on the address + the property description only, NOT the whole
        # page: nav/footer words like "New Developments" were mis-typing normal homes.
        t = guess_type(rec.get("address", "") + " " + (meta.get("description") or ""))
        if t:
            rec["type"] = t

    beds = parse_beds(blob)
    if beds is not None:
        rec["beds"] = beds
    status = parse_status(blob)
    if status:
        rec["status"] = status
    # Mark deliberately-unpriced listings so downstream shows "POA" instead of £0.
    if not rec.get("price") and looks_poa(blob):
        rec["price_qualifier"] = "POA"
    # A short description snippet -- feeds the engine's planning-potential detection
    # (barn conversion / prior approval / development opportunity). Capped to keep the
    # committed JSON small; keyword detection needs the gist, not the full brochure.
    desc = rec.get("description") or meta.get("description") or text
    if desc:
        rec["desc"] = re.sub(r"\s+", " ", desc).strip()[:600]
    rec.pop("description", None)
    return rec


# ---------------------------------------------------------------------------
# State (snapshot) -- powers diffing + price-reduction signal
# ---------------------------------------------------------------------------

def load_state(path: str = STATE_FILE) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"listings": {}}  # url -> record


def save_state(state: dict, path: str = STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Per-agent run
# ---------------------------------------------------------------------------

def _looks_like_detail(url: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    return any(re.search(p, url) for p in patterns)


def gather_candidate_urls(fetcher: Fetcher, agent: dict) -> list[dict]:
    """Return [{loc, lastmod}] candidate property URLs for one agent."""
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(entry):
        loc = entry.get("loc")
        if loc and loc not in seen:
            seen.add(loc)
            candidates.append(entry)

    # 1) explicit sitemaps
    for sm in agent.get("sitemaps", []) or []:
        for e in parse_sitemap(fetcher, sm):
            add(e)

    # 2) discovery via robots.txt (only if asked, or nothing found yet)
    if agent.get("discover") or (not candidates and not agent.get("listing_pages")):
        # Derive a homepage to read robots.txt from. An explicit "home" is used
        # first -- this is the ONLY anchor a discover-only agent has (no sitemap,
        # no listing page), so without it discovery silently does nothing.
        home = (agent.get("home")
                or next(iter((agent.get("sitemaps") or [])
                             + (agent.get("listing_pages") or [])), None))
        if not home:
            fetcher.log(f"{agent.get('name','?')}: discover set but no home/sitemap/"
                        f"listing_pages anchor -- cannot find robots.txt, skipping")
        else:
            sms = fetcher.sitemaps_from_robots(home)
            if not sms:
                fetcher.log(f"{agent.get('name','?')}: robots.txt lists no sitemap at "
                            f"{urlparse(home).netloc}")
            for sm in sms:
                for e in parse_sitemap(fetcher, sm):
                    add(e)

    # 3) listing-page mode (auto-paginated: follows the 'next page' link)
    lp_patterns = agent.get("detail_patterns", [])
    for lp in agent.get("listing_pages", []) or []:
        base = f"{urlparse(lp).scheme}://{urlparse(lp).netloc}"
        for e in crawl_listing_pages(fetcher, lp, base, lp_patterns):
            add(e)

    # filter to detail pages
    patterns = agent.get("detail_patterns", [])
    filtered = [e for e in candidates if _looks_like_detail(e["loc"], patterns)]
    if len(filtered) > MAX_URLS_PER_AGENT:
        fetcher.log(f"{agent['name']}: {len(filtered)} URLs, capping to {MAX_URLS_PER_AGENT}")
        filtered = filtered[:MAX_URLS_PER_AGENT]
    return filtered


def run_agent(fetcher: Fetcher, agent: dict, state: dict) -> tuple[int, int]:
    """Fetch/refresh one agent. Returns (new_or_changed_fetched, total_live)."""
    listings = state.setdefault("listings", {})
    name = agent["name"]
    fetcher.log(f"--- {name} ---")

    candidates = gather_candidate_urls(fetcher, agent)
    if not candidates:
        fetcher.log(f"{name}: no candidate URLs found")
        return (0, 0)

    live_urls = {e["loc"] for e in candidates}

    # Mark listings that have disappeared as inactive (kept for history)
    for url, rec in listings.items():
        if rec.get("agent") == agent["key"]:
            rec["active"] = url in live_urls

    # Decide what to (re)fetch: new URLs, or ones whose lastmod changed.
    to_fetch = []
    for e in candidates:
        url, lastmod = e["loc"], e.get("lastmod")
        prev = listings.get(url)
        if prev is None:
            to_fetch.append(e)
        elif lastmod and lastmod != prev.get("lastmod"):
            to_fetch.append(e)
    # Newest-first if we have lastmod, so fresh stock is prioritised under budget.
    to_fetch.sort(key=lambda e: e.get("lastmod") or "", reverse=True)

    budget = MAX_DETAIL_FETCHES_PER_AGENT
    fetched = 0
    for e in to_fetch:
        if fetcher.is_dead(e["loc"]):
            break
        if fetched >= budget:
            fetcher.log(f"{name}: hit per-run fetch budget ({budget}); rest backfills next run")
            break
        resp = fetcher.get(e["loc"])
        if resp is None:
            continue
        rec = extract_listing(resp.text, e["loc"])
        rec["agent"] = agent["key"]
        rec["agent_name"] = name
        rec["lastmod"] = e.get("lastmod")
        rec["active"] = True
        prev = listings.get(e["loc"])
        # price-reduction signal
        if prev and prev.get("price") and rec.get("price"):
            if rec["price"] < prev["price"]:
                rec["price_reduced"] = True
                rec["previous_price"] = prev["price"]
            rec["first_seen"] = prev.get("first_seen")
        rec["first_seen"] = rec.get("first_seen") or _now_iso()
        listings[e["loc"]] = rec
        fetched += 1

    total_live = sum(1 for u in live_urls if listings.get(u))
    fetcher.log(f"{name}: fetched {fetched} new/changed; {total_live} live listings tracked")
    return (fetched, total_live)


def _now_iso() -> str:
    # Local-import safe timestamp (avoids issues in restricted runners).
    import datetime
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# If run.py uses different field names, remap here.
NORMALISED_KEYS = ("address", "postcode", "price", "type", "link")


def _to_output(rec: dict) -> dict:
    # Clean at EMIT time, not just at extraction: listings fetched before this logic
    # existed are served from state unchanged, so re-clean here every run. Stripping the
    # "| Agent Name" tail reveals the address's own outward code, which lets us reject a
    # previously-stored agent-office postcode (e.g. GU7 1HR) without re-fetching the page.
    addr = _strip_agent_suffix(html.unescape(rec.get("address") or ""))
    pc = _resolve_postcode(addr, rec.get("postcode") or "", "")
    out = {
        "address": addr or None,
        "postcode": pc or None,
        "price": rec.get("price"),
        "price_qualifier": rec.get("price_qualifier"),  # "POA" when deliberately unpriced
        "type": rec.get("type"),
        "link": rec.get("link"),
        "desc": rec.get("desc"),                        # short snippet for planning-signal detection
        # extras (safe to ignore downstream)
        "source": rec.get("agent_name"),
        "beds": rec.get("beds"),
        "status": rec.get("status"),
        "price_reduced": rec.get("price_reduced", False),
        "previous_price": rec.get("previous_price"),
        "first_seen": rec.get("first_seen"),
        "lastmod": rec.get("lastmod"),
    }
    return out


def fetch_agent_listings(agents: list[dict] | None = None,
                         state_file: str = STATE_FILE,
                         write_output: bool = True) -> list[dict]:
    """Main entry point. Returns active listings in PropertyScout shape."""
    agents = agents if agents is not None else [a for a in AGENTS if a.get("enabled")]
    fetcher = Fetcher()
    state = load_state(state_file)

    for agent in agents:
        try:
            run_agent(fetcher, agent, state)
        except Exception as e:  # never let one agent kill the run
            fetcher.log(f"{agent.get('name','?')}: unexpected error {e!r}")
        save_state(state, state_file)  # persist after each agent (crash-safe)

    # Emit only ACTIVE listings that have at least a link + (price or address).
    results = []
    for url, rec in state.get("listings", {}).items():
        if not rec.get("active"):
            continue
        if not (rec.get("price") or rec.get("address")):
            continue
        results.append(_to_output(rec))

    # Stable order: reduced first, then newest.
    results.sort(key=lambda r: (not r.get("price_reduced"), r.get("first_seen") or ""),
                 reverse=False)

    if write_output:
        try:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=1)
            fetcher.log(f"wrote {len(results)} listings -> {OUTPUT_FILE}")
        except Exception as e:
            fetcher.log(f"could not write {OUTPUT_FILE}: {e}")

    fetcher.log(f"done. dead domains this run: {sorted(fetcher._dead_domains) or 'none'}")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="PropertyScout local-agent fetcher")
    ap.add_argument("--selftest", action="store_true",
                    help="Fetch ONE permissive agent, 1 page, to smoke-test extraction")
    ap.add_argument("--list", action="store_true", help="List the agent registry and exit")
    ap.add_argument("--agent", help="Run a single agent by key")
    args = ap.parse_args(argv)

    if args.list:
        for a in AGENTS:
            flag = "on " if a.get("enabled") else "off"
            print(f"[{flag}] {a['key']:24s} {a['name']}")
        return 0

    if args.selftest:
        global MAX_DETAIL_FETCHES_PER_AGENT
        MAX_DETAIL_FETCHES_PER_AGENT = 1
        agent = next(a for a in AGENTS if a["key"] == "andrew_lodge")
        res = fetch_agent_listings(agents=[agent], state_file="selftest_state.json",
                                   write_output=False)
        print(json.dumps(res[:3], ensure_ascii=False, indent=2))
        print(f"\n{len(res)} listing(s) tracked; showing up to 3.")
        return 0

    if args.agent:
        agent = next((a for a in AGENTS if a["key"] == args.agent), None)
        if not agent:
            print(f"no such agent: {args.agent}", file=sys.stderr)
            return 2
        res = fetch_agent_listings(agents=[agent])
        print(f"{len(res)} listings")
        return 0

    res = fetch_agent_listings()
    print(f"{len(res)} listings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
