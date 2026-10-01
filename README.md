# Leadflow

Local foundation for an agency lead CRM. Product requirements and delivery order live in
[PRD.md](PRD.md) and [TASKS.md](TASKS.md).

The application provides a real API/database connection and a standalone Telegram bot
process. Stage 2 adds database models, shared contact validation, transactional lead
creation and persistent bot draft operations. CRM login, HTTP lead routes, lead forms,
the list and Telegram intake handlers are implemented in later stages.

Deployment decisions and API/bot interfaces are recorded in
[docs/contracts.md](docs/contracts.md). The documented lead and authentication endpoints are not available yet.

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
docker compose up -d api frontend
```

The environment script creates `.env` with random local credentials and leaves an existing
file unchanged. Never commit it or paste its contents into chat. `.env.example` contains
the supported configuration keys without secrets.

Open <http://localhost:5173>. The foundation page checks the actual API and database;
it shows a retry action if either is unavailable. It does not contain demonstration leads.
The API is also available at <http://localhost:8000/api/health/>.

If a host port is occupied, change `FRONTEND_PORT` or `API_PORT` in `.env` before starting.
Only local loopback ports are published; PostgreSQL has no host port.

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

Public signup, user profile APIs and token issuance routes are absent. Future CRM APIs
require authentication by default. Only the minimal health endpoint is public.
Create a technical administrator interactively:

```sh
docker compose run --rm api python manage.py createsuperuser
```

Open <http://localhost:8000/admin/>. This technical login is separate from the future
demonstration-password login for the CRM.


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
cover successful health requests, rejected responses, timeouts and cancellation.
The frontend build is written to `frontend/dist/`, which is ignored by Git.

For a manual recovery check, stop `api`, press the connection-check button in the browser,
then start `api` and press **Повторить**. The page must show the error and then recover.

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
- `frontend/src/`: foundation screen and cancellable API requests.
- `frontend/src/theme.ts`: shared Ant Design theme settings.
- `frontend/tests/`: client request tests using Node's built-in test runner.
- `compose.yaml`: local services and persistent volumes.
- `scripts/init_local_env.py`: local environment initialization.

Lead validation and transactional persistence use shared synchronous server
operations under the CRM app. HTTP handlers and bot handlers call those operations rather
than duplicating them. Async bot code calls transactional Django operations through
`sync_to_async(thread_sensitive=True)`, with connection cleanup inside the synchronous
boundary. Draft data lives in PostgreSQL. Completed drafts are removed in the same transaction
that stores their lead and submission receipt; BotUser retains the last receipt reference.

Public deployment is handled after the working MVP is ready. Local Django development
settings and the Vite development server are not deployment configuration.
