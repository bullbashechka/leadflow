# Leadflow

Local foundation for an agency lead CRM. Product requirements and delivery order live in
[PRD.md](PRD.md) and [TASKS.md](TASKS.md).

The application provides a real API/database connection and a standalone Telegram bot
process. Stage 2 adds database models, shared contact validation, transactional lead
creation and persistent bot draft operations. Stage 3 adds the shared-password CRM login,
absolute 48-hour sessions and a protected browser shell. HTTP lead routes, lead forms,
the list and Telegram intake handlers remain later stages.

Deployment decisions and API/bot interfaces are recorded in
[docs/contracts.md](docs/contracts.md). Authentication endpoints are available; lead and
tag endpoints are planned for stage 4.

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
python3 scripts/set_demo_password.py
docker compose up -d api frontend
```

The environment script creates `.env` with random local credentials and leaves an existing
file unchanged. Never commit it or paste its contents into chat. `.env.example` contains
the supported configuration keys without secrets.

The password command asks twice without echoing the input. It writes only the encoded hash
to the ignored `.env`, with quoting that preserves its dollar signs. An absent or malformed
hash disables login. No password is sent through frontend build variables.

Open <http://localhost:5173> and enter the password you chose. The protected shell checks
the actual API and database; it shows a retry action if either is unavailable. It has no lead
list, lead forms or demonstration leads yet.
The API is also available at <http://localhost:8000/api/health/>.

If a host port is occupied, change `FRONTEND_PORT` or `API_PORT` in `.env` before starting.
Only local loopback ports are published; PostgreSQL has no host port. Set
`DJANGO_CSRF_TRUSTED_ORIGINS` to the exact frontend origin when changing its port.

## CRM access and password changes

For an existing local environment, set or change the demo password and recreate only API:

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
and localStorage for a pending-logout marker. No password, token or form contents are stored
in localStorage. Reload does not restore unfinished forms. Product behavior belongs to
[PRD.md](PRD.md#доступ-к-crm).

Remove expired sessions and old login counters periodically:

```sh
docker compose run --rm api python manage.py cleanup_crm_auth
```

The login limiter uses the immediate network peer until deployment establishes a trusted
proxy chain. Its cloud source-address check belongs to stage 8; never enable trust for
arbitrary client forwarding headers.

## Telegram bot

Add `BOT_TOKEN` to the local `.env`, then start the bot:

```sh
docker compose --profile bot up -d bot
docker compose --profile bot ps
```

Open your bot in Telegram and send `/start`. At this stage it explains that lead intake is
not yet available. Run exactly one polling process per bot token. Stop the local bot before
using the same token in a later deployment. Do not configure a webhook for this polling bot.

API and bot use the same backend image, Django settings, and database. Verify the database
from the bot service, even without a token:

```sh
docker compose --profile bot run --rm bot python manage.py runbot --check-db
```

Missing, malformed or rejected tokens stop startup with a message that does not include
the token. Stop the bot after changing its token and recreate it with the command above.

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

`bot.services` also provides draft start/resume/restart, field changes, review corrections,
contact removal, question binding and cancellation. These are internal synchronous
operations, not Telegram handlers. Transport callers must provide verified sender identity
and current draft UUID/revision. Text/phone input must include its question event binding;
`event=None` is reserved for trusted internal calls without a bound question.

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
docker compose run --rm api pytest -q
docker compose run --rm api ruff check .
docker compose run --rm api ruff format --check .
docker compose run --rm frontend npm test
docker compose run --rm frontend npm run lint
docker compose run --rm frontend npm run build
```

Backend tests use a separate PostgreSQL test database. Unit tests replace Telegram calls;
starting the bot with a real token remains a separate connectivity check. Frontend tests
cover API requests, access state, expiry, offline recovery and stale-response handling.
The frontend build is written to `frontend/dist/`, which is ignored by Git.

For a manual recovery check, interrupt the API connection in browser developer tools.
Already validated access stays visible until expiry, with a connection warning. Logout
hides content immediately and remains pending across reload. Restore connectivity;
logout must finish before another login is possible.

### Browser access checks

Initialize `.env` and build the local images as described in [First start](#first-start).
Install the optional host test dependencies with Node.js 24, then run from the repository root:

```sh
npm --prefix frontend ci
npm --prefix frontend exec -- playwright install chromium
docker compose up -d --wait postgres
python3 scripts/test_auth_browser.py
```

The runner requires the running local PostgreSQL service and free loopback ports 18003
and 15173. It does not start or stop PostgreSQL. It creates a
temporary PostgreSQL database, applies migrations there, supplies random test credentials,
starts separate API/frontend containers and runs Playwright at widths 375 and 1440.
It stops its containers and drops only its own database after the checks. Credentials are
not printed or written to repository files. Screenshots stay in a system temporary directory
and are removed by default. To inspect them, provide `--artifacts /tmp/leadflow-auth-review`
and remove that directory when finished.

The checks run in Chromium. The main journey uses the real API. Failure scenarios mock
network responses. Standard frontend build and lint commands also check the browser-test
configuration, scenarios and TSX fixture. The isolated
form fixture verifies retained text and operation identity without creating leads; the
production entry does not import it. Repeat those scenarios with actual forms in stage 4.

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

- `docs/contracts.md`: stage 1 deployment, session, API and bot contracts for later implementation.
- `backend/config/`: Django settings and routes, adapted from Cookiecutter Django.
- `backend/leadflow/users/`: generated custom user model and technical admin.
- `backend/leadflow/crm/`: health check, models, migrations, shared validation and transactional submission services.
- `backend/leadflow/bot/`: command handlers, polling command, persistent user/draft models and dialogue services.
- `backend/leadflow/database.py`: database probe shared by HTTP and the bot.
- `backend/tests/`: API, access, admin and bot tests.
- `backend/leadflow/crm/api/`: session endpoints, demo permissions, CSRF and JSON errors.
- `frontend/src/`: login, protected shell, access controller and cancellable API requests.
- `frontend/src/theme.ts`: shared Ant Design theme settings.
- `frontend/tests/`: Node client tests, Playwright journeys and a test-only form fixture.
- `compose.yaml`: local services and persistent volumes.
- `scripts/init_local_env.py`: local environment initialization.
- `scripts/set_demo_password.py`: interactive local password-hash setup.
- `scripts/test_auth_browser.py`: isolated browser-check runner.

Lead validation and transactional persistence use shared synchronous server
operations under the CRM app. HTTP handlers and bot handlers call those operations rather
than duplicating them. Async bot code calls transactional Django operations through
`sync_to_async(thread_sensitive=True)`, with connection cleanup inside the synchronous
boundary. Draft data lives in PostgreSQL. Completed drafts are removed in the same transaction
that stores their lead and submission receipt; BotUser retains the last receipt reference.

Public deployment is handled after the working MVP is ready. Local Django development
settings and the Vite development server are not deployment configuration.
