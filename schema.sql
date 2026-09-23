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
    active          BOOLEAN NOT NULL DEFAULT TRUE,   -- archived, not deleted, once a band has show history
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Matching on name happens in the app (case/whitespace/"The"-insensitive,
-- same normalization the artifact prototype used) rather than a DB
-- constraint, because two different real acts can share a stage name.

-- A multi-day hold: several candidate dates for the same prospective
-- show, each free to sit at its own point on the hold ladder (1st hold
-- on one date, 3rd on another). Deliberately a bare anchor with no
-- columns of its own — the group's "primary" date is whichever member
-- has the lowest id (created first), computed on the fly rather than
-- stored, so there's no second field that could disagree with reality.
-- That primary date owns the acts/deal/tasks/messages/tiers/staff/
-- settlement; the other dates in the group are lightweight placeholders
-- that borrow its data for display (see app.py's _group_anchor_id).
-- The moment any date is confirmed, it inherits everything and every
-- other date in the group is deleted outright — no "didn't work out"
-- trail for the ones that didn't happen (see _migrate_shared_event_data).
CREATE TABLE hold_groups (
    id          SERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE events (
    id              SERIAL PRIMARY KEY,
    venue_id        INTEGER NOT NULL REFERENCES venues(id),
    show_date       DATE NOT NULL,
    doors           TIME,
    show_time       TIME,
    hold_group_id   INTEGER REFERENCES hold_groups(id) ON DELETE SET NULL,

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
    ticket_link     TEXT,
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
    notes       TEXT,              -- anything this band needs for THIS show specifically
    -- A band that can't do it isn't the same as one that hasn't answered
    -- yet — declined stays visually struck through and sorts to the
    -- bottom of the bill, but the row (and its contact history) stays
    -- put rather than being deleted, so nobody re-contacts them not
    -- realizing someone already got a no.
    declined    BOOLEAN NOT NULL DEFAULT FALSE,
    -- This band's role on THIS bill — separate from the artist's own
    -- overall tier (Local/Regional/National), which describes the band
    -- everywhere, not just tonight's lineup.
    bill_role   TEXT CHECK (bill_role IN ('Touring', 'Direct Support', 'Support', 'Local') OR bill_role IS NULL),
    set_time      TIME,             -- when this act actually plays, for the printed set time sheet
    set_time_end  TIME,             -- NULL = open-ended ("10:30-?"), true for every headliner
    UNIQUE (event_id, artist_id)
);

-- Who reached out to this band about this show, how, and when — so two
-- bookers don't both contact the same band without realizing it.
CREATE TABLE event_artist_contacts (
    id              SERIAL PRIMARY KEY,
    event_artist_id INTEGER NOT NULL REFERENCES event_artists(id) ON DELETE CASCADE,
    method          TEXT NOT NULL CHECK (method IN ('text', 'email', 'phone', 'messenger')),
    person_id       INTEGER REFERENCES people(id),
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
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
    id              SERIAL PRIMARY KEY,
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    role_id         INTEGER NOT NULL REFERENCES roles(id),
    person_id       INTEGER REFERENCES people(id),   -- NULL = role open, unfilled
    scheduled_time  TIME,                            -- when this shift is supposed to start
    clocked_in_at   TIMESTAMPTZ,
    clocked_out_at  TIMESTAMPTZ,
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

-- Two boards, one table: Vision Board ("idea" — a flat list of loose
-- concepts) and MAO/Mutually-Agreeable Offers ("offer_sent" — one stage,
-- not a pipeline). Which board a card lives on is derived from column_key
-- alone, never stored separately, so a card can't end up on the wrong
-- board disagreeing with its own stage. No "booked" stage — once a show
-- is actually booked it lives on the Calendar, tracking it here too would
-- just be the same fact twice.
--
-- trigger_event_id is the "strike while it's hot" case Broc described: a
-- local band opens a packed show and gets real exposure, so the follow-up
-- ask should be ready to go, linked back to the show that created the
-- opening rather than living only in someone's memory.
CREATE TABLE vision_cards (
    id                SERIAL PRIMARY KEY,
    title             TEXT NOT NULL,
    column_key        TEXT NOT NULL DEFAULT 'idea'
                      CHECK (column_key IN ('idea', 'offer_sent')),
    sort_order        INTEGER NOT NULL DEFAULT 0,
    artist_id         INTEGER REFERENCES artists(id),
    trigger_event_id  INTEGER REFERENCES events(id) ON DELETE SET NULL,
    notes             TEXT,
    link              TEXT,
    follow_up_date    DATE,
    follow_up_person_id INTEGER REFERENCES people(id),  -- who the follow-up bell notifies
    created_by        INTEGER REFERENCES people(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_vision_cards_column ON vision_cards(column_key, sort_order);
CREATE INDEX idx_vision_cards_follow_up ON vision_cards(follow_up_date) WHERE follow_up_date IS NOT NULL;

-- A band can have more than one point of contact (the singer, the manager,
-- whoever actually answers) — this is the list of them, separate from the
-- single legacy contact_name/email/phone columns on artists itself.
CREATE TABLE artist_members (
    id          SERIAL PRIMARY KEY,
    artist_id   INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    phone       TEXT,
    email       TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- A running comment thread on one show — booker/owner coordination
-- ("did we confirm the guarantee yet?", "door needs to know load-in time"),
-- not a general chat app. Fetched on demand when a show's editor opens,
-- not bundled into the main events list — unlike tasks/staff this can
-- grow unbounded over a show's life and shouldn't bloat every calendar load.
CREATE TABLE event_messages (
    id                  SERIAL PRIMARY KEY,
    event_id            INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    person_id           INTEGER REFERENCES people(id),
    body                TEXT NOT NULL,
    parent_message_id   INTEGER REFERENCES event_messages(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_event_messages_event ON event_messages(event_id, created_at);
CREATE INDEX idx_event_messages_parent ON event_messages(parent_message_id) WHERE parent_message_id IS NOT NULL;

-- @mentions inside a message body — "you need to look at this." Marked
-- read as a side effect of opening that show's message thread, not a
-- separate click. Real push/email notifications don't exist in this app;
-- this only surfaces as a badge count next time the mentioned person is
-- actually using ConcertPro.
CREATE TABLE event_message_mentions (
    id          SERIAL PRIMARY KEY,
    message_id  INTEGER NOT NULL REFERENCES event_messages(id) ON DELETE CASCADE,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    read_at     TIMESTAMPTZ
);
CREATE INDEX idx_message_mentions_unread ON event_message_mentions(person_id) WHERE read_at IS NULL;

-- A lightweight "seen this, no reply needed" — separate from a mention's
-- read_at, which is a private per-recipient inbox flag. An ack is public:
-- everyone in the thread sees who's acknowledged a message.
CREATE TABLE event_message_acks (
    message_id  INTEGER NOT NULL REFERENCES event_messages(id) ON DELETE CASCADE,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (message_id, person_id)
);

-- General operational to-dos ("order ticket stock", "fix the marquee
-- sign") — deliberately separate from event_tasks, which is the per-show
-- booking checklist and stays scoped to one show. A todo is not tied to
-- any show. Crew can see and check off only the ones assigned to them
-- (same one-function-decides shape as events_for); booker/owner see and
-- assign all of them.
CREATE TABLE todos (
    id            SERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    done          BOOLEAN NOT NULL DEFAULT FALSE,
    assigned_to   INTEGER REFERENCES people(id),
    due_date      DATE,
    notes         TEXT,
    created_by    INTEGER REFERENCES people(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_todos_assigned ON todos(assigned_to) WHERE NOT done;

-- Offers, contracts, flyers, riders — stored in Backblaze B2 (see storage.py),
-- this table is only the pointer to that object plus who/what it belongs to.
-- event_id NULL means it lives in the general library, not a show's folder.
-- Booker/owner only for now (contracts are exactly the kind of financial
-- document crew shouldn't see) — no crew-facing use case has come up yet,
-- so there's no permissions.py function to write until one does.
CREATE TABLE event_files (
    id              SERIAL PRIMARY KEY,
    event_id        INTEGER REFERENCES events(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    storage_key     TEXT NOT NULL UNIQUE,
    content_type    TEXT,
    size_bytes      BIGINT,
    uploaded_by     INTEGER REFERENCES people(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_event_files_event ON event_files(event_id);

-- The public site's per-show extras. Deliberately NOT a copy of venue/
-- date/doors/lineup/ticket_link/price — those already live on events/
-- event_artists/ticket_tiers and the site reads them from there, so
-- there's no second place they could disagree with the booking data.
-- Only what's genuinely site-only lives here: which uploaded file is the
-- hero image, the headliner blurb, and whether the show is live on the
-- public site at all.
CREATE TABLE event_website (
    event_id        INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    hero_file_id    INTEGER REFERENCES event_files(id) ON DELETE SET NULL,
    blurb           TEXT,
    published       BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed data matching what's already live in the artifact prototype.
INSERT INTO venues (name) VALUES ('Frankies'), ('Ottawa Tavern'), ('Cla-Zel Theater');
INSERT INTO roles (name) VALUES
    ('Sound'), ('Door'), ('Promoter Rep'), ('Merch'), ('Security'), ('Bartender'), ('Manager');
