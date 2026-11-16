-- Migration 0015: adopted sources (M5; portal-api.md section 5.10,
-- POST /v1/adopt; catalog-seeding.md section 1).
--
-- An adopted source is data Prizmanyx already moved into the dataset's own
-- backend: seeded like a migration source, but with no migration (the
-- dataset stays as it is) and with origin 'adopt' on its rows.

ALTER TABLE catalog.source
    ADD COLUMN kind text NOT NULL DEFAULT 'source' CHECK (kind IN ('source', 'adopt'));
