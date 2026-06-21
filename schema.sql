-- PropertyScout persistence schema.
-- One row per canonical property, keyed on a hash of address + postcode.
-- The DB is committed to the repo each run, so price_history and
-- days-on-market come for free from successive scrapes.

CREATE TABLE IF NOT EXISTS properties (
    id              TEXT PRIMARY KEY,        -- sha1(address+postcode)[:8]
    address         TEXT NOT NULL,
    postcode        TEXT,
    lat             REAL,
    lng             REAL,
    property_type   TEXT,
    beds            INTEGER,
    price           INTEGER,
    first_seen      TEXT,                    -- ISO date
    last_seen       TEXT,                    -- ISO date, updated every run
    days_on_market  INTEGER,
    status          TEXT DEFAULT 'live',     -- live | sstc | withdrawn
    relisted_count  INTEGER DEFAULT 0,
    source_portal   TEXT,
    source_listing_id TEXT,
    source_url      TEXT,
    source_agent    TEXT,
    photo_count     INTEGER,
    has_floorplan   INTEGER,                 -- 0 / 1
    thumb_url       TEXT,
    description_raw TEXT,
    enrichment_json TEXT,                    -- serialised enrichment block
    comps_json      TEXT,                    -- serialised comparables
    signals_json    TEXT,                    -- serialised score breakdown
    flags_json      TEXT,                    -- serialised flag list
    score           INTEGER,
    low_comp        INTEGER DEFAULT 0,
    scored_at       TEXT
);

-- Price changes captured across runs. Append-only.
CREATE TABLE IF NOT EXISTS price_history (
    id              TEXT,
    date            TEXT,
    price           INTEGER,
    FOREIGN KEY (id) REFERENCES properties(id)
);

CREATE INDEX IF NOT EXISTS idx_score ON properties(score DESC);
CREATE INDEX IF NOT EXISTS idx_status ON properties(status);
