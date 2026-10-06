# Leadflow

Local foundation for an agency lead CRM. Product requirements and delivery order live in
[PRD.md](PRD.md) and [TASKS.md](TASKS.md).

The application provides a real API/database connection and a standalone Telegram bot
process. The CRM supports manual and bot intake, automatic list refresh, lead editing,
statuses, notes, tag management, combined search and filters, lead deletion, and clearly
marked fictional demo records.

Deployment decisions and API/bot interfaces are recorded in
[docs/contracts.md](docs/contracts.md). Authentication, tag, and lead endpoints are
available to an authenticated CRM session.

## Prerequisites

- Docker Desktop with Docker Compose, running.
- Python 3 on the host, only for generating local environment values.
- A Telegram bot token from BotFather, only for the optional bot service.

Application dependencies run inside Docker. The backend uses Python 3.14, Django, DRF,
PostgreSQL 17 and aiogram 3. The frontend uses Node.js 24.13, React, TypeScript, Vite,
and Ant Design 6 with a Russian locale. Global theme settings live in `frontend/src/theme.ts`.
Exact dependencies are recorded in `backend/uv.lock` and `frontend/package-lock.json`.
Cookiecutter Django revision and generation options are recorded in `backend/generation.json`.

## First start

Run from the repository root:

```sh
python3 scripts/init_local_env.py
docker compose build
docker compose up -d --wait postgres
docker compose run --rm api python manage.py migrate
docker compose run --rm api python manage.py seed_demo_leads
python3 scripts/set_demo_password.py
docker compose up -d api frontend
```

The environment script creates `.env` with random local credentials and leaves an existing
file unchanged. Never commit it or paste its contents into chat. `.env.example` contains
the supported configuration keys without secrets.

The password command asks twice without echoing the input. It writes only the encoded hash
to the ignored `.env`, with quoting that preserves its dollar signs. An absent or malformed
hash disables login. No password is sent through frontend build variables.

Open <http://localhost:5173> and enter the password you chose. The CRM loads persisted
leads and tags from the API. The one-time seed command adds six fictional leads and does
not restore records after edits or deletion. On an existing environment, apply migrations
and run the same seed command; it safely skips when demo records were already seeded.
To show the public bot link, set `VITE_TELEGRAM_BOT_URL` in
`.env` to its `https://t.me/<bot_username>` address. This public URL can be included in the
browser bundle; keep `BOT_TOKEN` on the server.
The API is also available at <http://localhost:8000/api/health/>.

If a host port is occupied, change `FRONTEND_PORT` or `API_PORT` in `.env` before starting.
Only local loopback ports are published; PostgreSQL has no host port. Set
`DJANGO_CSRF_TRUSTED_ORIGINS` to the exact frontend origin when changing its port.

## CRM access and password changes

Production uses individual operator accounts. There is no public signup; staff and
superuser accounts remain separate from CRM. See [production configuration](docs/production.md)
for account provisioning, trusted ingress, TLS and release steps. Deactivate an operator
or change their password to revoke their sessions permanently, including after reactivation
or restoring old credentials. Changes to staff or superuser status also revoke CRM access. Historical and bot receipts can have
no CRM actor; attributed receipts keep their original actor on retries.

Local settings default to explicit demo mode. For an existing local environment, set or
change the demo password and recreate only API:

```sh
python3 scripts/set_demo_password.py
docker compose up -d --no-deps --force-recreate api
```

The helper adds the local trusted frontend origins if that key is absent. Existing origin
settings stay unchanged. Changing the encoded hash revokes earlier CRM sessions on their
next server check. Recreate every API instance after changing its environment. Keep
`DJANGO_SECRET_KEY` stable across normal restarts.

The compatibility target is current Chrome, Firefox and Safari. Automated browser checks
currently cover Chromium; Firefox and Safari remain unverified.
Production requires HTTPS; localhost
HTTP is supported only for development. The browser uses Web Locks to serialize session
operations across tabs, BroadcastChannel with storage-event fallback for notifications,
and localStorage plus a host-only cookie for a 48-hour pending-logout marker. If both persistence channels fail, access stays closed. No password, token or form
contents are stored in either channel. Reload does not restore unfinished forms. Product behavior belongs to
[PRD.md](PRD.md#доступ-к-crm).

Remove expired sessions and old login counters periodically:

```sh
docker compose run --rm api python manage.py cleanup_crm_auth
```

CRM login permits 10 attempts per source, 20 per account and 60 globally in each
60-second window. Counters contain keyed digests. The login limiter uses a verified source address when trusted ingress is configured,
otherwise the immediate peer. Admin has a separate limiter and is network-closed by
default in production. Never trust arbitrary client forwarding headers. Operational
checks and backup/restore instructions are in [operations](docs/operations.md).

## Telegram bot

Add `BOT_TOKEN` to the local `.env`, then start the bot:

```sh
docker compose --profile bot up -d bot
docker compose --profile bot ps
```

For an existing local environment, pause API writes and bot polling before changing the
lead schema. Then apply migrations and seed the fictional review records once:

```sh
docker compose --profile bot stop bot
docker compose stop api
docker compose run --rm api python manage.py migrate
docker compose run --rm api python manage.py seed_demo_leads
docker compose up -d api
```

If you use the bot, start it again with its updated shared lead rules:

```sh
docker compose --profile bot up -d --force-recreate bot
```

Open your bot in Telegram and send `/start`. The bot collects a draft, lets the user review
and correct it, then saves the confirmed lead to the same PostgreSQL database used by CRM.
Run exactly one polling process per bot token. Do not configure a webhook for this bot.
The update offset, pending submission and unsent bot replies are stored in PostgreSQL so
the bot can resume after restart. Telegram delivery can repeat a prompt if the process stops
after Telegram accepted it but before the database recorded delivery; draft updates and lead
creation remain protected against duplicate processing.

API and bot use the same backend image, Django settings, and database. Verify the database
from the bot service, even without a token:

```sh
docker compose --profile bot run --rm bot python manage.py runbot --check-db
```

Missing, malformed or rejected tokens stop startup with a message that does not include
the token. Recreate the one existing bot service after changing its token.

Run the bot intake checks against the isolated PostgreSQL test database:

```sh
docker compose run --rm api pytest -q tests/test_bot.py tests/test_bot_transport.py tests/test_drafts.py
```

These tests use synthetic Telegram updates and do not send real messages. A live Telegram
check is part of the later end-to-end acceptance stage.

## Django Admin

Public signup, user profile APIs and token issuance routes are absent. CRM APIs require
demo access by default. Only health and session discovery/login/logout are public;
login and logout still require CSRF.
Create a technical administrator interactively:

```sh
docker compose run --rm api python manage.py createsuperuser
```

Open <http://localhost:8000/admin/>. This technical login is separate from CRM access.
The internal `__leadflow_demo__` principal has no usable normal password or Admin rights.


## Stage 2 server operations

Apply migrations with the first-start command above before using the server operations.
The data migration adds four system tags once. Re-running `migrate` does not reseed data.
Django Admin exposes tags for inspection; mutations use domain operations.

`crm.services.create_lead` creates a manual submission from its UUID and payload.
`bot.services.confirm_draft` uses the locked current review to create a bot submission.
Both use the same field validators and CRM transaction. Each success returns a lead and
a replay flag. Reuse the UUID and original payload after an unknown result; a new UUID
means a new submission even when contacts match. PostgreSQL owns concurrency protection.

`bot.services` provides draft start/resume/restart, field changes, review corrections,
contact removal, question binding and cancellation. The Telegram transport calls these
shared operations; it does not duplicate contact validation or lead persistence. Text/phone
input must include its question event binding; `event=None` is reserved for trusted internal
calls without a bound question.

Run the focused stage 2 checks:

```sh
docker compose run --rm api pytest -q tests/test_validation.py tests/test_crm_storage.py tests/test_drafts.py tests/test_storage_races.py
```

Race tests use independent PostgreSQL connections and real transaction commits/rollbacks.
Restart verification used a separate Compose project and volume with fictitious data,
a PostgreSQL restart and fresh API/bot service containers. No Telegram messages were sent.
Product rules belong to PRD; contact formats and future JSON contracts belong to
[docs/contracts.md](docs/contracts.md).

## Checks

After changing frontend dependencies, refresh the existing dependency volume. Rebuilding
the image does not replace an already initialized `node_modules` volume:

```sh
docker compose run --rm --no-deps frontend npm ci
```

```sh
docker compose config --quiet
docker compose run --rm api python manage.py check
docker compose run --rm api python manage.py makemigrations --check --dry-run
docker compose run --rm api pytest -q --ds=config.settings.test
docker compose run --rm api ruff check .
docker compose run --rm api ruff format --check .
docker compose run --rm frontend npm test
docker compose run --rm frontend npm run lint
docker compose run --rm frontend npm run build
```

Backend tests use a separate PostgreSQL test database.
The explicit `--ds` overrides the local container settings. For concurrent test runs,
set `LEADFLOW_TEST_DATABASE` to a different test database name for each process.
Unit tests replace Telegram calls; starting the bot with a real token remains a separate
connectivity check. Frontend tests
cover API requests, access state, expiry, offline recovery and stale-response handling.
The frontend build is written to `frontend/dist/`, which is ignored by Git.

For a manual recovery check, interrupt the API connection in browser developer tools.
Already validated access stays visible until expiry, with a connection warning. Logout
hides content immediately and remains pending across reload. Restore connectivity;
logout must finish before another login is possible.

### Browser access and CRM refresh checks

Initialize `.env` and build the local images as described in [First start](#first-start).
Install the optional host test dependencies with Node.js 24, then run from the repository root:

```sh
npm --prefix frontend ci
npm --prefix frontend exec -- playwright install chromium
docker compose up -d --wait postgres
python3 scripts/test_auth_browser.py
python3 scripts/test_auth_browser.py --individual --grep 'real API|persisted external'
```

The runner requires the running local PostgreSQL service and free loopback ports 18003
and 15173. It does not start or stop PostgreSQL. It creates a
temporary PostgreSQL database, applies migrations there, supplies random test credentials,
starts separate API/frontend containers and runs Playwright at widths 375 and 1440.
The `--individual` run creates a temporary non-staff operator and verifies personal
login, edit, status changes, deletion and replay against the real API. It gives each
browser project a separate database so focused checks retain the real login limit.
It stops its containers and drops only its own database after the checks. Credentials are
not printed or written to repository files. Screenshots stay in a system temporary directory
and are removed by default. To inspect them, provide `--artifacts /tmp/leadflow-auth-review`
and remove that directory when finished.
Run a focused refresh check with `python3 scripts/test_auth_browser.py --grep 'auto-refresh|persisted external'`.

The checks run in Chromium. The main journeys use the real API for authentication,
workspace loading, contact review, manual creation, tag filtering and unsaved-form exit.
Failure scenarios and an unknown create outcome use mocked network responses. The latter
commits one fake lead in the mock and drops its reply before verifying that a retry reuses
the same operation. Refresh journeys cover incoming leads, tag matching, retained loaded
pages and scroll, drafts, independent tab counters, hidden-tab suspension and recovery.
A real-API journey measures a persisted external creation against the ten-second target
and checks idempotent replay. It does not send Telegram messages. The real-bot acceptance
check in TASKS.md remains separate. Standard frontend build and lint commands also check the browser-test
configuration, scenarios and TSX fixture. The separate form fixture still verifies
reauthentication state without creating leads; the production entry does not import it.

## Stop and restart

```sh
docker compose --profile bot down
docker compose up -d api frontend
docker compose --profile bot up -d bot
```

The named PostgreSQL volume retains data across restarts and container recreation.
Do not use `down --volumes` unless you intend to delete the local database.
Migration commands are explicit; API and bot never apply migrations automatically.

## Repository map

- `docs/contracts.md`: deployment, session, API and bot contracts.
- `backend/config/`: Django settings and routes, adapted from Cookiecutter Django.
- `backend/leadflow/users/`: generated custom user model and technical admin.
- `backend/leadflow/crm/`: health check, models, migrations, shared validation, and transactional lead and tag operations.
- `backend/leadflow/bot/`: Telegram dialogue handlers, durable polling offset, processed-update log, reply outbox, drafts and dialogue services.
- `backend/leadflow/database.py`: database probe shared by HTTP and the bot.
- `backend/tests/`: API, access, admin, draft and synthetic Telegram-transport tests.
- `backend/leadflow/crm/api/`: session endpoints, demo permissions, CSRF and JSON errors.
- `frontend/src/`: login, protected shell, access controller and cancellable API requests.
- `frontend/src/CRMWorkspace.tsx`: view selection, responsive detail placement, scroll restoration and request coordination.
- `frontend/src/LeadListView.tsx`, `LeadDetails.tsx`, `ManualLeadForm.tsx`: filtered list, editable detail and manual intake views.
- `frontend/src/LeadEditForm.tsx`, `LeadStatusControl.tsx`, `TagManagerModal.tsx`: lead changes and tag management.
- `frontend/src/leadList.ts`: serialized list polling, stable arrival cursors, counters and retained pagination.
- `frontend/src/theme.ts`: shared Ant Design theme settings.
- `frontend/tests/`: Node client tests, Playwright journeys and a test-only form fixture.
- `docs/design/`: approved CRM references and previews made with test data.
- `compose.yaml`: local services and persistent volumes.
- `scripts/init_local_env.py`: local environment initialization.
- `scripts/set_demo_password.py`: interactive local password-hash setup.
- `backend/leadflow/crm/management/commands/seed_demo_leads.py`: one-time creation of fictional demo leads.
- `scripts/test_auth_browser.py`: isolated browser-check runner.

Lead validation and transactional persistence use shared synchronous server
operations under the CRM app. HTTP handlers and bot handlers call those operations rather
than duplicating them. Async bot code calls transactional Django operations through
`sync_to_async(thread_sensitive=True)`, with connection cleanup inside the synchronous
boundary. Draft data lives in PostgreSQL. Completed drafts are removed in the same transaction
that stores their lead and submission receipt; BotUser retains the last receipt reference.

Public deployment is handled after the working MVP is ready. Local Django development
settings and the Vite development server are not deployment configuration.
