# desmos cold store on D1

The far end of the outbox. Push-only, idempotent on `fingerprint`, and
deliberately dumb: it inserts facts and never updates or deletes one.

## Deploy

    wrangler d1 create desmos-cold-store          # paste the id into wrangler.toml
    wrangler d1 execute desmos-cold-store --file schema.sql --remote
    wrangler secret put DESMOS_SYNC_TOKEN
    wrangler deploy

Then on the machine that syncs:

    export DESMOS_D1_URL=https://<worker>.workers.dev/
    export DESMOS_D1_TOKEN=<the same secret>

`desmos.state.d1.push(world)` drains one batch. With no `DESMOS_D1_URL` it
reports itself unconfigured and touches nothing.

## What is unverified

Cloudflare's published limits -- D1 row size, request body size, statements
per `batch()`, and free-plan write quotas -- have **not** been read from
their docs. `MAX_ROWS = 200` is a guess chosen to be small, not a limit
derived from anything. If a real deployment refuses a batch, the client
leaves it pending and the correct fix is to lower the batch, not to drop
rows.

## What is verified

`desmos check` executes `schema.sql` against SQLite and asserts the columns
cover every field the client puts on the wire, and that the insert is
`INSERT OR IGNORE` on the fingerprint. That catches the drift that would
actually hurt: the client learning a field the table has no column for.
