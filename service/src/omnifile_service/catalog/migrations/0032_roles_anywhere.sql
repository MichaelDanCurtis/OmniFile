-- Migration 0032: IAM Roles Anywhere certificates from OmniFile's edge CA
-- (decision 50; control-api.md section 2, threat model T4).
--
-- An edge asks `api` for a Roles Anywhere certificate for its TPM-sealed AWS
-- key (POST /v0/edges/{id}/aws-certificate); `enrol` signs it with the edge
-- CA. Every certificate issued is kept here, so a revoked edge's unexpired
-- ones go into the CA's CRL, which the worker uploads to each configured
-- trust anchor (ImportCrl the first time, UpdateCrl after).

-- The edge's AWS key, pinned at its first Roles Anywhere certificate: SHA-256
-- of the SubjectPublicKeyInfo DER. A CSR for another key is refused.
ALTER TABLE catalog.edge ADD COLUMN ra_key_sha256 bytea
    CHECK (ra_key_sha256 IS NULL OR length(ra_key_sha256) = 32);

CREATE TABLE catalog.edge_ra_certificate (
    serial       text PRIMARY KEY CHECK (serial ~ '^[0-9a-f]{1,40}$'),
    edge_id      bigint NOT NULL REFERENCES catalog.edge (edge_id),
    cert_sha256  bytea NOT NULL CHECK (length(cert_sha256) = 32),
    key_sha256   bytea NOT NULL CHECK (length(key_sha256) = 32),
    not_after    timestamptz NOT NULL,
    issued_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX edge_ra_certificate_edge ON catalog.edge_ra_certificate (edge_id, not_after);

-- What each trust anchor (an operator setting, OMNIFILE_AWS_ACCOUNTS) was
-- last given. `published_through` is the latest revocation the published
-- CRL holds: a revoked edge with an unexpired certificate revoked after it
-- means the CRL is due. Written by the worker only.
CREATE TABLE catalog.roles_anywhere_crl (
    trust_anchor_arn   text PRIMARY KEY,
    crl_id             text,
    crl_number         numeric(40, 0),
    published_at       timestamptz,
    published_through  timestamptz,
    attempts           integer NOT NULL DEFAULT 0,
    failing_since      timestamptz,
    next_attempt_at    timestamptz,
    last_error         text
);

-- Sent in the same transaction as the revocation (decision 27).
SELECT pgmq.create('roles_anywhere_crl');
SELECT pgmq.create('roles_anywhere_crl_dlq');
