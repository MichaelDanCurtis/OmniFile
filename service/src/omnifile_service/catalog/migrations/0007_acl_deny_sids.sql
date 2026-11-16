-- POSIX.1e access-check order for "can this AD user read this ACL?"
-- (decision 28; control API §4.6). `read_sids` alone over-grants: a
-- `user:X:---` entry, or a matching group entry without r, must stop X even
-- when a group of X or `other` grants read.
--
-- POSIX.1e (and Linux acl(5)) decide in this order, first match wins:
--   1. the caller is the owner          -> the owner entry;
--   2. a named-user entry matches       -> that entry, masked;
--   3. any group entry matches (owning group or named groups):
--      read if ANY matching one grants r (masked), else denied;
--   4. otherwise                        -> the `other` entry.
--
-- The service stores, per ACL:
--   read_sids : users decided "read" at steps 1-2, groups granting r at step
--               3, and Everyone (S-1-1-0) when `other` has r. The GIN index
--               on it stays the prefilter: every reader matches it.
--   deny_sids : users decided "no read" at steps 1-2, and groups with an
--               entry that doesn't grant r (only effective when no matching
--               group grants).
-- catalog.acl_can_read() then gives the exact answer.

ALTER TABLE catalog.acl
    ADD COLUMN deny_sids catalog.sid[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN catalog.acl.read_sids IS
    'Users granted read at the owner/named-user step, groups granting read, and S-1-1-0 when other has r. A superset filter: use catalog.acl_can_read() for the answer.';
COMMENT ON COLUMN catalog.acl.deny_sids IS
    'Users denied read at the owner/named-user step, and groups whose matching entry does not grant r.';

-- Can a user (SID, plus its transitive group SIDs) read through this ACL?
-- User and group SIDs never collide (AD SIDs are unique), so one set each
-- holds both kinds; Everyone is the `other` step, not a group.
CREATE FUNCTION catalog.acl_can_read(p_read catalog.sid[], p_deny catalog.sid[],
                                     p_user text, p_groups text[])
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        -- steps 1-2: the user is the owner or a named user
        WHEN p_user = ANY (p_deny::text[]) THEN false
        WHEN p_user = ANY (p_read::text[]) THEN true
        -- step 3: a matching group grants, else a matching group denies
        WHEN coalesce(p_groups, '{}') && array_remove(p_read::text[], 'S-1-1-0') THEN true
        WHEN coalesce(p_groups, '{}') && p_deny::text[] THEN false
        -- step 4: other
        ELSE 'S-1-1-0' = ANY (p_read::text[])
    END
$$;
