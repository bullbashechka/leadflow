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

## First HR release (2026-10-06)

The public CRM and cloud bot are available for HR review. Real Telegram submissions
with a manual email and the user's explicitly authorized phone arrived in the open
CRM within 10 seconds. The cloud bot owns one polling lease.
[TASKS](../TASKS.md) owns the remaining acceptance checklist; this release does not
close every stage 8 check.

| Component | Address or release |
| --- | --- |
| CRM | <https://leadflow.bullbashechka.workers.dev> |
| Telegram bot | <https://t.me/leadflowhh_bot> |
| API health | <https://api-production-4fa8.up.railway.app/api/health/> |
| Application source | `1c86b94f0605b219a22032c3ccc16f0e73c503a1` |
| API deployment | `20164e08-f5fe-4830-9e5b-61926770b8e5` |
| Bot deployment | `b7ca1aab-62c4-4fb6-b50d-84ada462a1bd` |
| Worker version | `bd61ac95-deb2-43a1-a6a9-3cdcb674d8f8` |
| Worker source | `41eea3e`; Worker/frontend unchanged by `1c86b94` |
| Migration deployment | `594153b9-97e1-4804-a26a-0d4fec6b8c69`; temporary service removed |

### Provisioned resources

- Railway project: `e0579f90-4277-483e-a261-4e66acf5327f` (`leadflow`).
- Environment: `bef34935-bf41-4387-82d7-4ca48a88a123` (`production`).
- API service: `001fec2d-59b0-45e1-bfc6-2ab67b14414e`.
- Bot service: `3db8ac9d-94ff-4e2f-920b-d0005cd4ef39`.
- PostgreSQL service: `6f0ed82f-fb8a-4428-a1f6-816a962dc824`.
- Database deployment: `94398696-f3f2-4a3b-95fa-6680a90cacac`.
- Database volume: `832f2179-0375-495c-a42d-42d8d4b4ca1a`, mounted at
  `/var/lib/postgresql/data`; `PGDATA=/var/lib/postgresql/data/pgdata`.
- PostgreSQL image: `ghcr.io/railwayapp-templates/postgres-ssl:17@sha256:ee908c46d659fe2853fd16ea47635b6c65f91936d66b7f8a2346f8cca575e36b`.
- Region: `europe-west4-drams3a` (Amsterdam). The deployment manifest records this
  under `multiRegionConfig`; the legacy `region` response can be null.
- Cloudflare account: `0b481a63e36d7f8a4b5058544213d90e`; Worker: `leadflow`, Free tier.

API and bot use one replica, no sleep, a 45-second drain window, and manual releases.
GitHub autodeploy is disconnected. The API has one Gunicorn worker and two threads.
The bot has no public domain. Local Compose polling is stopped; do not start it while
cloud polling runs. Telegram updates were not flushed during the switch.

The database uses its private hostname `postgres.railway.internal`. Certificate
verification passed for that hostname and failed with an incorrect hostname or CA,
including after PostgreSQL restart. Connections negotiate TLS 1.3. The public TCP
proxy list is empty. All 36 migrations ran in the removed one-off task, with the
separate migration timeout profile. API and bot startup do not apply migrations.

The new database contains six fictional demo leads and four system tags. No local
records or drafts were copied. Repeating `seed_demo_leads` left six leads unchanged.
Only our acceptance records were deleted. Operator `demo` is active, with neither
staff nor superuser privileges. The generated password passed existing validation.
The schema also contains its built-in `__leadflow_demo__` receipt identity. It has
an unusable password and is explicitly rejected by individual authentication; it
does not provide a second CRM login.

Server variables live in Railway. Private ignored files `.env.release-state.json`
and `.env.hr-access.txt` have mode `0600`. Never print or commit their contents.
The access file contains the credentials and a short HR review scenario.

### Verified proxy boundary

The deployed trust list is `127.0.0.1/32,::1/128,100.64.0.0/16`. Health and external
requests showed immediate peers `100.64.0.1` through `100.64.0.5`, with the external
peer changing between requests. A [Railway employee statement](https://station.railway.com/questions/unexpected-egress-spike-causing-2-3x-mon-01458b94)
confirms `100.64.x.x` proxy traffic. The ingress secret is required in addition to
peer trust. Do not treat the current range as a permanent platform guarantee.
Recheck actual peers after region or platform changes; stop public CRM ingress if
the boundary cannot be confirmed.

[Railway's HTTP specification](https://docs.railway.com/networking/public-networking/specs-and-limits)
states that it supplies `X-Forwarded-Proto: https`. External checks with `http`,
`https`, `garbage`, and `http,https` supplied by the client all reached Django as
HTTPS without a redirect. The checks passed again after API replacement and database
restart. Direct CRM and Admin requests, including forged ingress headers, returned
403. Admin's network allowlist remains empty.

### Checks and limits

- 401 backend tests passed with PostgreSQL. Ruff and format checks passed.
- 78 client/Worker tests, ESLint, TypeScript/Vite build, Django checks, migration
  drift checks, production Docker builds and dependency advisory checks passed.
- 108 isolated browser tests passed; 14 viewport-specific cases skipped. Eight
  additional individual-account browser tests passed.
- Public login/logout/session journeys passed at desktop and phone widths. CSRF
  rejection, exact origin enforcement, Secure/HttpOnly/SameSite=Lax cookies, and
  uncached API responses passed before and after service replacement.
- Public desktop and phone journeys passed for tagged/untagged creation, contact
  links, filters/reset, hidden created leads, preserved form values, two-tab edit
  conflicts, deletion cancellation/confirmation, and no resurrection on replay.
- Public tag journeys passed at both widths: creation, removing an assignment
  from one lead, deleting a custom tag from all remaining leads without losing
  them, and protecting system tags.
- A response was dropped after a real cloud save. Retrying the frozen submission
  created one lead; a distinct new submission created a distinct lead.
- Public validation rejected blank fields, invalid contacts and overlong values
  with field errors, without creating records. Root and nested SPA URLs returned
  the same application shell. Final screens were visually inspected; keyboard
  focus and absence of horizontal page overflow were checked at both widths.
- PostgreSQL restart preserved all six leads and four tags. API and bot replacement
  preserved them. The bot stopped on lease connection loss and recovered one
  polling owner through supervision. A second contender was rejected before any
  Telegram call. The queue contains no pending or failed work.
- CA and idle scheduling regressions observed RED then GREEN. Inverted conditions
  were detected by their tests. Independent backend/frontend reviews had no
  remaining required changes.
- Real Telegram acceptance passed for multiple directions, invalid manual contact
  rejection, manual email entry, phone sharing and separate new intake. Corrections
  to the original request and contact messages, the name and directions reached
  the saved lead. The email lead had the website and automation tags.
- A populated draft survived a cloud bot restart with the same submission ID and
  values. `/start` offered its saved data; continuing reached the same request.
  An old closed-draft button reported that it was stale.
- The email lead appeared in an already open CRM after 4.982 seconds; the phone lead
  appeared after 5.336 seconds. Timing starts immediately before the native submit
  click and ends when the browser sees the row, without a manual reload. Double
  clicking email confirmation left one lead and one receipt. Phone arrival retained
  active search and direction conditions and the text in a separate open form.
  Its card contained a valid `tel:` link. No phone value was written to repository
  files or browser evidence screenshots.
- Repeating `/start` on an empty draft offered continue/restart/cancel. Restart
  replaced the draft ID. Cancelling a populated synthetic draft showed a confirmation;
  choosing continue preserved its request and cleared the pending action. Final
  cleanup used the existing server cancellation operation after the Telegram window
  changed chats. It does not prove the final Telegram delete-button callback.
- Both acceptance leads were deleted through the public authenticated API. Their
  receipts retained deletion markers and cleared payloads. Final state was six demo
  leads, four system tags, no active drafts, and no pending or failed bot deliveries.

Remaining manual Telegram checks are an account without a username, the final
populated-draft cancellation callback and restarting a populated draft. Local
PostgreSQL/transport tests cover these contracts. Session expiration and
password revocation use deterministic local tests; no 48-hour production wait or
production password rotation was performed. Deleted demo seed behavior was tested
locally; the six cloud demo records were preserved. Production backups and the
stage 9 delivery document remain deferred. The build reports a large client chunk
(about 362 KiB gzip); its phone load and layout passed acceptance. HSTS preload is
not enabled for provider-owned hostnames.

### Resource observation

At 08:30 UTC, warmed idle readings were about 200 MB and 0.030 vCPU for the bot,
58 MB and 0.031 vCPU for PostgreSQL, and 94 MB with near-zero idle CPU for API.
The volume used 136 MB. Active browser checks increased API CPU temporarily.
Before idle backoff, bot/PostgreSQL used about 0.159/0.186 vCPU while idle.
The bot now checks an empty delivery queue once per second, while active delivery
keeps its 0.1-second cadence. Submission contracts and transport limits are unchanged.

At [current Railway rates](https://docs.railway.com/pricing/plans), this steady idle
sample projects about $4.9/month in resource use: memory about $3.5, CPU about $1.2,
and storage about $0.02, plus small egress. This is a short observation, not a monthly
billing guarantee; active usage, growing memory and restarts can raise the total.
Hobby includes $5 of resource use in its $5 subscription. Actual resource usage at
08:28 UTC was about $0.0063. Warning remains $5 and hard limit $10 for the workspace.
Do not raise the limit automatically. Use provider usage for the actual bill;
its first-day projection is not a stable forecast.

After real Telegram and browser checks, the 09:20–09:25 UTC sample showed about
202 MB / 0.029 vCPU for bot, 85 MB / 0.039 vCPU for PostgreSQL, and 116 MB /
0.0004 vCPU for API. Applying the same rates projects about $5.4/month in resource
use, before small egress. Resource usage reported around 09:28 UTC was $0.01293.
The workspace still reported warning $5, hard limit $10, and no limit breach.

### Restore service availability without deleting data

Use the existing project and volumes. Do not create a new database, flush Telegram
updates, reverse migrations, or run `seed_demo_leads` as recovery.

```sh
release_project=e0579f90-4277-483e-a261-4e66acf5327f
release_api=001fec2d-59b0-45e1-bfc6-2ab67b14414e
release_bot=3db8ac9d-94ff-4e2f-920b-d0005cd4ef39
release_postgres=6f0ed82f-fb8a-4428-a1f6-816a962dc824

railway logs --project "$release_project" --environment production --service "$release_api" --lines 30
railway ssh --project "$release_project" --environment production --service "$release_api" -- python manage.py release_status
railway metrics --project "$release_project" --environment production --all --since 5m --json
railway usage --workspace 32d6b0f1-e3c8-45ad-ba12-321fc57efefc --json
```

Restart only the affected service. If PostgreSQL is unavailable, restart its process
first and wait for verified database connectivity, then restart API and bot if needed.

```sh
railway restart --project "$release_project" --environment production --service "$release_postgres" --yes
railway restart --project "$release_project" --environment production --service "$release_api" --yes
railway restart --project "$release_project" --environment production --service "$release_bot" --yes
```

For a broken image, stop only the affected application deployment with `railway down`
and upload a compatible verified revision with the existing service settings and
variables. Keep the PostgreSQL service and volume intact. Inspect any schema difference
before selecting an older application revision. Image replacement does not undo writes.

```sh
railway down --project "$release_project" --environment production --service "$release_bot" --yes
# Run from the verified source checkout, after fixing the issue:
railway up ./backend --path-as-root --project "$release_project" --environment production --service "$release_bot"
# Release API separately, then Worker as described in production.md.
```

After recovery, check `release_status`, public login/CSRF/ingress, retained data and
one polling owner. No production backup exists for restoring lost or deleted data.
