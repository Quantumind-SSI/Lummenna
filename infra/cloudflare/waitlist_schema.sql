-- Cloudflare D1 schema for the download waitlist.
--
-- Applied with:
--   wrangler d1 create lummenna-waitlist
--   wrangler d1 execute lummenna-waitlist --remote --file infra/cloudflare/waitlist_schema.sql
--
-- Data minimisation is deliberate. Only what is needed to honour the stated
-- purpose ("send the download link when it ships") is stored: the address, the
-- consent decision, when it was given, and which page it came from. No IP
-- address, no user agent, no tracking identifier. That keeps the lawful basis
-- narrow and makes a UK GDPR erasure request a single DELETE.

CREATE TABLE IF NOT EXISTS waitlist_signup (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Stored lower-cased and trimmed so the UNIQUE constraint actually
    -- de-duplicates; addresses are case-insensitive in practice.
    email        TEXT    NOT NULL UNIQUE,

    -- Recorded rather than assumed. A row without explicit consent must never
    -- be created, but storing the decision means it can be evidenced later.
    consent      INTEGER NOT NULL CHECK (consent IN (0, 1)),

    -- Unix seconds, UTC. Evidence of when consent was given.
    created_at   INTEGER NOT NULL,

    -- Which page the signup came from, for attribution only.
    source       TEXT    NOT NULL DEFAULT '/',

    -- Set when the download link is actually sent, so a later mail-out can
    -- select only the people who have not yet been contacted.
    notified_at  INTEGER
);

-- Supports "who has not been emailed yet", the only bulk query this table needs.
CREATE INDEX IF NOT EXISTS idx_waitlist_pending
    ON waitlist_signup (notified_at)
    WHERE notified_at IS NULL;

-- Supports chronological export and retention pruning.
CREATE INDEX IF NOT EXISTS idx_waitlist_created
    ON waitlist_signup (created_at);
