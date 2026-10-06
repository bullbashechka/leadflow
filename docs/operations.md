# Release and recovery runbook

Use the setup commands in [README](../README.md). This guide covers release
acceptance, private status checks, backup verification, and failure handling.
Do not use local Compose settings for a public deployment.

## Before release

1. Run the backend tests, migration consistency check, lint, and Django deploy
   checks. Run frontend tests, lint, build, Worker tests, and browser journeys.
2. Check both locked dependency sets:

   ```sh
   python -B -m unittest discover -s scripts/tests
   python -B scripts/check_dependencies.py
   ```

   The checker uses the public PyPI version API and npm bulk advisory API. It
   includes development and optional dependencies. Exit code `0` means these
   sources returned no known advisories. Code `1` means findings. Code `2` means
   an incomplete check; it must not be treated as a clean result. Keep proxy and
   TLS verification enabled. Resolve findings or unavailable checks before release.

3. Test the first release through the real Worker and Railway proxy chain before
   accepting real leads. Use provider-generated addresses and synthetic data;
   a permanent cloud staging environment is not required. Verify login, logout, CSRF rejection, two separate
   `Set-Cookie` headers, `no-store` API responses, and database TLS verification.
4. Verify direct API access cannot bypass authenticated ingress. Verify the
   direct admin address cannot bypass its network restriction. Check that the
   configured trusted proxy ranges describe the actual platform boundary.
5. Verify HTTPS redirects, secure cookies, HSTS, resource policies, and the absence
   of debug error pages. Do not enable a strict resource policy without checking
   the complete UI at phone and desktop widths.
6. Record the deferred production backup limitation in the HR demonstration handoff.
   Run the isolated local recovery rehearsal after schema changes. Before extending
   use beyond this demonstration, configure backups and perform the production drill. Do not put credentials or
   customer records in the release record.

## Release order

1. Record the image digest and Worker version. Keep the previous artifacts.
2. For this initial HR demonstration, record that production backups are deferred.
   Do not claim a supported recovery point or recovery time. For later releases with
   real operational data, confirm a recoverable backup before rollout.
3. Apply additive database migrations before replacing API or bot processes.
   On Railway, execute migrations once with the intended production configuration
   and the separate timeout profile in [production](production.md#runtime-settings). Do not run independent migrations from API and bot
   startup. For a Compose rehearsal, use the production `migrate` service:

   ```sh
   docker compose --env-file .env.production -f compose.production.yaml run --rm migrate
   ```

4. Start the API and verify its health. Start the bot and confirm that only one
   process owns polling. A second process must not send messages.
5. Release the Worker and static frontend. Keep the fixed API origin and ingress
   secret out of frontend build variables.
6. Perform the external acceptance journeys against the released versions. Test
   with synthetic leads, then remove them through the normal CRM operation.
7. Check queue age and errors during the initial observation period.

Do not reverse authentication or database migrations as a generic rollback.
Confirm compatibility with the previous image first. If it is incompatible,
pause writes and fix forward. Never restore the shared demo login to bypass an
authentication failure.

## Private operational status

Run this command in a backend container with the intended configuration:

```sh
python manage.py release_status
python manage.py release_status --max-pending-age 300
```

The command reads database state and emits JSON aggregates only:

| Field under `bot` | Meaning |
| --- | --- |
| `pending` | Messages that still await delivery. |
| `due` | Pending messages whose individual retry time has arrived. A global cooldown can still prevent delivery. |
| `failed` | Total messages marked as permanently failed. Compare successive readings to detect new failures. |
| `oldest_pending_age_seconds` | Age of the oldest pending message; zero for an empty queue. |
| `last_processed_update_age_seconds` | Age of the newest processed update; null if none exists. An idle bot is not necessarily broken. |
| `cooldown_seconds` | Maximum remaining Telegram cooldown among stored bot states. |
| `pending_submissions` | Drafts awaiting completion of submission. |

The age-limited command exits unsuccessfully when the threshold is exceeded.
It prints aggregates before that exit. A database read failure produces a generic
error and no partial status. It does not prove that a polling process is alive.
Use process supervision and queue trends for that check. Do not publish this
command as an anonymous HTTP endpoint.

Set initial queue-age alerts to five minutes, then adjust from observed normal
delivery time. Investigate a growing queue, increasing failed count, repeated
cooldowns, lost polling ownership, health failures, or sustained API errors.
Account for a reported Telegram cooldown before restarting the bot. A restart
must not bypass the persisted cooldown.

Use `/api/health/` for process availability; it runs no database query. Use
`/api/readiness/` with a valid CRM session through authenticated ingress to check
database connectivity. A public health `200` does
not prove login, Worker cookie forwarding, or Telegram delivery works. Keep a
separate authenticated synthetic journey for that purpose.

Keep request bodies, search queries, cookies, passwords, tokens, ingress secrets,
and Telegram messages out of logs. Do not enable verbose HTTP client logging in
production. Limit log access and retention. Review provider logging settings
before launch; application configuration cannot control all provider logs.

## Local dump and restore rehearsal

Start local PostgreSQL and API services, then run:

```sh
python -B scripts/test_db_restore.py
```

The script uses the local Docker socket and local `postgres` and `api` services.
It creates two UUID-named databases, applies current migrations, and inserts
synthetic data. It uses `pg_dump` and `pg_restore`, verifies values and relational
links, and compares migration, constraint, and row counts. It replays creation
and deletion, verifies that a deleted submission cannot resurrect a lead, and
includes a pending bot message. The archive contains synthetic
data only and is removed automatically. Both databases are dropped on completion
or failure. The normal local database is not read or modified.

A successful local rehearsal validates the tooling and current schema. It does
not validate Railway backups, retention, permissions, encryption, or restore
speed. Repeat it after schema changes.

## Production backup and restore drill

Production backups are deferred for the initial HR demonstration. No scheduled
backup job or production restore drill is implemented. Data recovery is not
guaranteed. This limitation must remain visible in the handoff.

The future proposed schedule is one encrypted PostgreSQL backup every 48 hours,
with the last seven successful scheduled copies retained. Take an additional backup
before a release. The target recovery point is at most 48 hours of lost writes when
scheduled backups succeed; failed or overdue backups require an alert and retry.
Measure the recovery time in the drill before recording a supported recovery target.

Railway's built-in volume schedules are daily, weekly or monthly, so stage 8.1e
requires a separate Railway cron service and a private Railway Bucket for encrypted
logical dumps. The job must use a durable schedule marker, handle month boundaries,
prevent concurrent runs, retry failures and exit after each run. Do not use a
day-of-month `*/2` expression as an exact 48-hour schedule. Update the success marker
and remove old copies only after the new encrypted archive is stored successfully.
This job is planned, not implemented or enabled by the current repository.

Before expanding beyond the demonstration, configure encrypted backups with
restricted access and complete the drill.
Check the actual provider plan, retention, point-in-time recovery availability,
and backup coverage. Do not assume these features are enabled from repository
configuration. Keep backup access separate from ordinary operator access.

If a logical export is required, use a restricted PostgreSQL service profile and
password file with mode `0600`, `sslmode=verify-full`, and the trusted root CA.
Do not put a password-bearing connection URL in shell history or command
arguments. Store temporary plaintext archives only in a private directory,
encrypt them with the approved backup tool before remote storage, and delete the
plaintext after encryption succeeds. Never upload an unencrypted export.

For the actual drill:

1. Restore the chosen backup to a separate restricted database or provider
   project. Never restore over the running production database.
2. Run the matching image and migrations against that database. Use no real
   `BOT_TOKEN`. Disable outbound delivery and all external jobs.
3. Verify schema, constraints, counts, representative relational links, lead
   versions, submission tombstones, and operation receipts privately.
4. Check that replaying a known submission cannot create a duplicate or restore
   a deleted lead. Check the recovered bot offset and outbox state before any
   future polling is allowed.
5. Record restore duration and the newest recovered transaction time. Set the
   accepted recovery time and recovery point from this measurement.
6. Delete the isolated restore environment and its temporary exports. Confirm
   cleanup and retain only the aggregate drill result.

Restoring older polling and outbox state can replay updates and messages. During
an incident, stop existing bot processes before database switching. Inspect the
recovered state and possible deliveries after the restore point. Telegram does
not provide exactly-once delivery; a lost send response may still cause a repeat
message. Lead submission receipts must prevent duplicate leads.

## Failure handling

- **API unavailable:** check health, database connectivity, deploy checks, and
  the proxy boundary. Preserve unknown write outcomes; retry only the same
  operation identifier and snapshot after the user requests it.
- **Bot backlog:** read `release_status`, inspect cooldown and process ownership,
  then inspect sanitized error categories. Keep one active polling owner.
- **Credentials exposed:** revoke the affected credential and sessions, rotate
  secrets in their server-side stores, and inspect attributable mutations. Do
  not print the credential while diagnosing the incident.
- **Incorrect migration or data change:** stop affected writes, preserve the
  current state, restore to an isolated target, and validate a recovery plan
  before replacing the live database.

Actual production restore, public-domain acceptance, provider log review, and
secret configuration require access to the deployed environment. A successful
local check is not evidence that these operational steps are complete.


## Bot history maintenance

The delivery loop removes terminal transport events older than seven days hourly,
using batches of 1000. It preserves pending delivery, submission and warning references,
business receipts, polling offset and active drafts. Cleanup failure uses normal database
retry handling. Do not delete a draft to clear a backlog.

Run a bounded dry run or manual cleanup in the intended backend environment:

```sh
python manage.py cleanup_bot_history --dry-run
python manage.py cleanup_bot_history
```

Dry run reports eligible events in the first batch per bot, at most 1000 per bot;
it does not claim a total for a larger backlog. Normal cleanup drains eligible batches.
Monitor the database, queue age and cooldown with `release_status` before restarting.
