-- D1 schema for the desmos cold store.
--
-- One table, one primary key, and that key is the fingerprint the local
-- outbox already computed: sha256 over canonical JSON of kind and payload.
-- Redelivery is therefore a no-op up here as well as down there, which is
-- what makes the push idempotent without a transaction spanning the network.
--
-- Nothing in this schema can be updated by the sync. Rows arrive and stay.
CREATE TABLE IF NOT EXISTS cold_facts (
    fingerprint  TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cold_facts_workspace
    ON cold_facts(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cold_facts_kind
    ON cold_facts(kind, created_at);
