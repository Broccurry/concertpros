-- ConcertPros schema
--
-- Design note that matters more than any single table: settlements live in
-- their own table, joined only for 'booker'/'owner' queries, specifically so
-- a crew-level query can be "don't join settlements" rather than "select
-- these columns but not those" — the permission boundary is a JOIN that
-- either happens or doesn't, not a column list someone has to remember to
-- trim correctly on every new endpoint. Same reasoning applies to keeping
-- deal/hold fields on `events` visible only through status + role gating in
-- the one shared query function (see permissions.py), never re-implemented
-- per-endpoint.

CREATE TABLE people (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT,               -- NULL until the owner sets a temp password
    access_level    TEXT NOT NULL DEFAULT 'crew'
                        CHECK (access_level IN ('crew', 'booker', 'owner')),
    phone           TEXT,
    note            TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Job roles someone can cover on a show night (Sound, Door, Bartender, ...).
-- Deliberately NOT the same thing as people.access_level, which is the auth
-- permission tier. A person can hold many job roles; job roles have nothing
-- to do with what data they're allowed to see.
CREATE TABLE roles (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE person_roles (
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    role_id     INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (person_id, role_id)
);

CREATE TABLE venues (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE artists (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    tier            TEXT CHECK (tier IN ('Local', 'Regional', 'National') OR tier IS NULL),
    genre           TEXT,
    tags            TEXT[] NOT NULL DEFAULT '{}',  -- free-text, e.g. style/location — searchable, not a fixed list
    location        TEXT,
    contact_name    TEXT,
    email           TEXT,
    phone           TEXT,
    agent_name      TEXT,
    agent_company   TEXT,
    agent_email     TEXT,
    agent_phone     TEXT,
    mgmt_name       TEXT,
    mgmt_email      TEXT,
    mgmt_phone      TEXT,
    instagram       TEXT,
    facebook        TEXT,
    website         TEXT,
    spotify         TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Matching on name happens in the app (case/whitespace/"The"-insensitive,
-- same normalization the artifact prototype used) rather than a DB
-- constraint, because two different real acts can share a stage name.

CREATE TABLE events (
    id              SERIAL PRIMARY KEY,
    venue_id        INTEGER NOT NULL REFERENCES venues(id),
    show_date       DATE NOT NULL,
    doors           TIME,
    show_time       TIME,

    -- The hold ladder is a priority order, not an unordered category set —
    -- see legend/status rendering in the prototype. Crew never sees anything
    -- but 'confirmed' (and 'complete' for their own history); the other
    -- statuses exist for booker/owner only.
    status          TEXT NOT NULL DEFAULT 'hold1'
                        CHECK (status IN ('hold1', 'hold2', 'hold3', 'confirmed', 'complete', 'dead')),

    deal_type       TEXT,
    guarantee       NUMERIC,
    backend_pct     NUMERIC,
    deal_notes      TEXT,
    announce_date   DATE,
    onsale_date     DATE,
    notes           TEXT,

    created_by      INTEGER REFERENCES people(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Optimistic concurrency: two bookers editing the same show (Christian
    -- and Cody both open Oct 3) should get a conflict warning, not a silent
    -- last-write-wins that drops one of their edits. The app bumps this on
    -- every UPDATE and rejects a save whose expected version is stale.
    version         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_events_date ON events(show_date);
CREATE INDEX idx_events_venue ON events(venue_id);
CREATE INDEX idx_events_status ON events(status);

-- A show's bill: one row per act, in running order. Replaces the old
-- single artist_id + free-text `support` column — a show can have any
-- number of acts, each independently confirmed (a headliner can be
-- locked in while a support act is still a tentative hold). sort_order 0
-- is the headliner by convention.
CREATE TABLE event_artists (
    id          SERIAL PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    artist_id   INTEGER NOT NULL REFERENCES artists(id),
    confirmed   BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    -- This band's own deal for this show, separate from the event-level
    -- Deal fieldset (which is the promoter's overall terms for the night).
    -- guarantee is agreed beforehand; paid and walkups are filled in once
    -- the show is over and become that band's show history.
    guarantee   NUMERIC,
    paid        NUMERIC,
    walkups     INTEGER,
    UNIQUE (event_id, artist_id)
);

CREATE TABLE ticket_tiers (
    id          SERIAL PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    price       NUMERIC,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- The Website/Marketing/Offer/Contract checklist, spawned from a template
-- when a show is booked. Not visible to crew (booking-side operational
-- detail), but not financial either — kept off the settlements boundary.
CREATE TABLE event_tasks (
    id              SERIAL PRIMARY KEY,
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    label           TEXT NOT NULL,
    done            BOOLEAN NOT NULL DEFAULT FALSE,
    owner_person_id INTEGER REFERENCES people(id),
    sort_order      INTEGER NOT NULL DEFAULT 0
);

-- Who's working which job role on which show. A crew member's OWN rows here
-- are the one thing they're allowed to see about a show beyond the public
-- fields — see permissions.py.
CREATE TABLE assignments (
    id          SERIAL PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    role_id     INTEGER NOT NULL REFERENCES roles(id),
    person_id   INTEGER REFERENCES people(id),   -- NULL = role open, unfilled
    UNIQUE (event_id, role_id, person_id)
);

-- Deliberately its own table, 1:1 with events, so a crew-level query can
-- simply never JOIN here rather than trust a column allowlist. See the note
-- at the top of this file.
CREATE TABLE settlements (
    event_id        INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    tickets_sold    INTEGER,
    gross           NUMERIC,
    expenses        NUMERIC,
    artist_payout   NUMERIC,
    settled         BOOLEAN NOT NULL DEFAULT FALSE,
    notes           TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auth sessions. Long-lived on purpose (crew checking a schedule on their
-- phone should not be re-logging-in constantly) — the opposite tradeoff
-- from a shared SellHQ register, which needs fast per-transaction PINs.
CREATE TABLE sessions (
    token       TEXT PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ         -- NULL = no expiry
);
CREATE INDEX idx_sessions_person ON sessions(person_id);

-- "Things get easily lost" was the founding complaint about ClickUp — this
-- table exists specifically so "who changed this and when" always has an
-- answer. Written to by the app layer on every mutating action, not by a
-- DB trigger, so it can record a human-readable summary alongside the raw
-- diff.
CREATE TABLE audit_log (
    id          SERIAL PRIMARY KEY,
    person_id   INTEGER REFERENCES people(id),   -- NULL = system action
    entity_type TEXT NOT NULL,                   -- 'event' | 'artist' | 'person' | 'settlement' | ...
    entity_id   INTEGER NOT NULL,
    action      TEXT NOT NULL,                   -- 'create' | 'update' | 'delete' | 'status_change' | ...
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_created ON audit_log(created_at);

-- The vision board: rough, editable-anytime ideas/goals with a soft target
-- quarter, not a real commitment like a booking. quarter/year travel
-- together (both set or both null = "someday, no target yet") so sorting
-- and grouping by quarter never has to parse a free-text date out of a
-- string.
CREATE TABLE vision_notes (
    id              SERIAL PRIMARY KEY,
    content         TEXT NOT NULL,
    target_quarter  INTEGER CHECK (target_quarter BETWEEN 1 AND 4),
    target_year     INTEGER,
    created_by      INTEGER REFERENCES people(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Mutually-agreeable offers: sent, no fixed response deadline, but someone
-- still needs to check back on it. Separate from vision_notes on purpose —
-- these are live negotiations with a real follow-up date, not brainstorming.
-- artist_id is optional: a band not yet in the roster can still get an M/A
-- offer logged under a plain title.
CREATE TABLE ma_offers (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    artist_id       INTEGER REFERENCES artists(id),
    notes           TEXT,
    link            TEXT,               -- URL to the actual offer doc, if hosted elsewhere
    submitted_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    follow_up_date  DATE,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_by      INTEGER REFERENCES people(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_ma_offers_follow_up ON ma_offers(follow_up_date) WHERE status = 'open';

-- Seed data matching what's already live in the artifact prototype.
INSERT INTO venues (name) VALUES ('Frankies'), ('Ottawa Tavern'), ('Cla-Zel Theater');
INSERT INTO roles (name) VALUES
    ('Sound'), ('Door'), ('Promoter Rep'), ('Merch'), ('Security'), ('Bartender'), ('Manager');
