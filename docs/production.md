# Production configuration

Use this guide for the release artifacts. Use [operations.md](operations.md) for
monitoring, recovery and release acceptance. Commands alone do not prove a
deployment; use the recorded release checks in that runbook. [TASKS](../TASKS.md)
owns hosting choices and progress.

## Hosting and CLI access

Use one Railway production project in Amsterdam for the API, bot and PostgreSQL.
Host the SPA and its authenticated API proxy in Cloudflare Workers Static Assets.
Use provider-generated addresses for the first release. Start releases manually
through CLI from a verified GitHub revision. Do not enable independent autodeploys
that can bypass the release order below. Local checks and production acceptance
do not require a permanent cloud staging environment.

Check the local CLI sessions before configuring a release:

```sh
railway whoami
gh api user --jq .login
cd frontend
npx --yes wrangler@4.147.0 whoami
```

If a session is missing, authenticate interactively:

```sh
railway login --browserless
gh auth login
# Run from frontend:
npx --yes wrangler@4.147.0 login --device --browser=false
```

Approve the provider's login page in your own browser. Keep CLI credentials in
their local stores outside the repository. Account login does not prove access to
the intended project or deploy an application. Verify the Railway workspace,
project, environment and service, Cloudflare account and Worker, and GitHub repository
before any remote mutation. CLI commands can require sandbox permission to refresh
sessions outside the workspace.

## Runtime settings

The backend image defaults to `config.settings.production` and Gunicorn. Its
entrypoint checks configuration and collects Admin static files before starting
the HTTP server. The development image and `compose.yaml` explicitly use local
settings. Do not use the development target for a public deployment.

HSTS is enabled for one year. Browser preload is not enabled automatically;
domain ownership and long-term suitability require a deployment decision.
`check --deploy` reports the optional `security.W021` warning until preload is
enabled. Startup checks fail on errors and do not suppress that warning.

Supply these backend variables through the hosting secret store or an ignored
`.env.production` file:

| Variable | Requirement |
| --- | --- |
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` |
| `DJANGO_SECRET_KEY` | Random production secret, at least 50 characters; no development key |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated exact backend hostnames; no wildcard or URL |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Comma-separated exact HTTPS frontend origins; no path or wildcard |
| `DATABASE_URL` | PostgreSQL URL with `sslmode=verify-full` and `sslrootcert` pointing to a readable, valid CA file |
| `DATABASE_CA_PEM` | Optional PEM trust certificate from the verified database; startup writes it atomically to the URL's absolute `sslrootcert` path with mode `0600` |
| `DJANGO_TRUSTED_PROXY_CIDRS` | Explicit, verified immediate proxy networks; never `0.0.0.0/0` or `::/0` |
| `INGRESS_SHARED_SECRET` | Random printable ASCII secret of at least 32 characters, identical to the Worker secret |
| `CRM_AUTH_MODE` | `individual` in production |
| `DJANGO_ADMIN_NETWORK_ALLOWLIST` | Empty by default; only actual permitted network peers, if private access is configured |
| `BOT_TOKEN` | Bot process only; never a frontend value |
| `PORT` | HTTP listening port, defaults to `8000` |
| `DB_CONNECT_TIMEOUT_SECONDS` | Runtime default `3` |
| `DB_STATEMENT_TIMEOUT_MS` | Runtime default `8000` |
| `DB_LOCK_TIMEOUT_MS` | Runtime default `2000` |

For Compose, also set `DATABASE_CA_PATH` to the host CA file. The production
Compose file mounts it read-only at `/run/certs/database-root.pem`. Use that path
in `DATABASE_URL`. URL-encode database credentials and query parameter values.
For Railway, use the PostgreSQL service's private hostname in the same project and
environment. Provision a trusted database certificate for that exact hostname and
the matching CA file at the path used in the API and bot database URLs. Check the
actual deployed certificate; an SSL-enabled database image or a default
`DATABASE_URL` does not prove `verify-full` compatibility. Configure the database
certificate if needed; do not disable verification to make the connection work.
Keep the database on a persistent volume and leave public TCP access disabled.

For Railway, set `sslrootcert=/tmp/leadflow-certs/database-root.pem` and provide
`DATABASE_CA_PEM` through server variables. The entrypoint prepares this file before
Django checks. Invalid PEM stops startup with a generic error. Empty PEM preserves
support for an existing mounted certificate. Never put database credentials or PEM
contents in command arguments, build variables, or release logs.

Use positive timeout values. The explicit migration process uses the same TLS URL
with `DB_STATEMENT_TIMEOUT_MS=60000` and `DB_LOCK_TIMEOUT_MS=10000`; these values
must not replace the API or bot runtime limits. The Compose migrate service applies
that separate profile. On Railway, set them only for the one-off migration process.
A timed-out mutation returns a safe database error; the browser retains its operation
UUID and snapshot when the outcome is uncertain.

The application refuses unsafe production configuration at startup. It does not
fall back to a local database or shared demo access. Local settings default to demo
mode and respect an explicit `CRM_AUTH_MODE=individual`; test settings use demo mode
for compatibility unless a test overrides it.

## Authenticated ingress

Deploy the Worker at the external Cloudflare ingress. Configure its server-side
`API_ORIGIN` as the fixed HTTPS Railway origin, with no credentials, query or
path. Supply `INGRESS_SHARED_SECRET` as a Worker secret. Do not expose either
server secret through `VITE_*` variables.

The Worker accepts the client IP only from Cloudflare's external request
metadata (`request.cf` and `CF-Connecting-IP`). It discards client routing and
ingress headers, then supplies `X-Leadflow-Ingress-Secret` and
`X-Leadflow-Client-IP`. Missing platform metadata fails closed.

The backend authenticates that metadata only when the immediate peer is in the
verified proxy networks and the secret matches. Production CRM routes reject
requests without this proof, including requests to the direct Railway URL.
`/api/health/` is the exception. It returns only process status without a database query and supports
plain HTTP local probes without a redirect.

Before setting proxy CIDRs, verify the actual peer addresses and the platform's
handling of `X-Forwarded-Proto`. The platform must overwrite client values.
Do not guess broad private-network ranges to make a failing check pass.
Gunicorn does not interpret forwarded protocol headers; Django's first
middleware sanitizes them before HTTPS enforcement.

Production ignores unauthenticated forwarded client addresses for login limits
and Admin access. Admin is closed by default. An allowlist for a shared Railway
proxy would permit every client of that proxy: do not use it. Enable Admin only
through verified private access with an identifiable immediate peer. Otherwise
use authenticated management commands for operator provisioning.

## Build and release

Railway service settings live in `deploy/railway-api.json` and
`deploy/railway-bot.json`. These are inputs to the supported GraphQL API, not legacy
Railway config-as-code files. Railway uses `backend/Dockerfile.railway` because its
builder rejects BuildKit secret mounts. Keep its production dependencies and runtime
aligned with the standard Dockerfile. Apply each to its existing service:

```sh
python3 scripts/configure_railway_service.py deploy/railway-api.json --service-id API_SERVICE_ID --environment-id ENVIRONMENT_ID
python3 scripts/configure_railway_service.py deploy/railway-bot.json --service-id BOT_SERVICE_ID --environment-id ENVIRONMENT_ID
```

Upload `backend` as the build root from the verified release checkout:

```sh
railway up ./backend --path-as-root --project PROJECT_ID --environment production --service API_SERVICE_ID
```

Use the same source revision for the bot after stopping the previous poller. Do not
connect a GitHub autodeploy source. The manifests configure one instance, no sleep,
a 45-second drain window, and one API worker with the existing two threads.
Railway replaces the image entrypoint when a custom start command is set, so
every start command must explicitly invoke `/app/entrypoint.sh`, including the
one-off migration command. This prepares CA trust and runs production checks.

Build the backend production image:

```sh
docker build --target production -t leadflow-production-backend backend
```

In a managed environment with an outbound TLS proxy, pass its public trust
bundle as the optional BuildKit secret `proxy_ca`. Keep inherited proxy settings
and TLS verification enabled. If the proxy hostname is available only in the
outer hosts file, provide its verified address with `--add-host`. Do not use
host networking to bypass network policy. The trust bundle is not stored in the
image.

Build static frontend assets and test the Worker:

```sh
cd frontend
npm ci
npm test
npm run build
```

Only `VITE_TELEGRAM_BOT_URL` is a supported public build setting. The production
Docker target exports `/dist`, `/worker` and `/wrangler.jsonc`; it does not run a
web server. `public/_headers` is copied into the assets and configures CSP,
HSTS, frame protection, referrer policy and MIME protection. Ant Design needs
inline styles; script execution remains restricted to this origin.

After reviewing the target Cloudflare account, configure and publish the Worker
with the pinned CLI. These commands write to that account:

```sh
npx --yes wrangler@4.147.0 secret put INGRESS_SHARED_SECRET
npx --yes wrangler@4.147.0 deploy --var API_ORIGIN:https://YOUR-API-ORIGIN
```

Use `frontend/wrangler.jsonc`. Confirm the intended Worker name and custom domain
before publication. The proxy has one 15-second deadline for request buffering and the upstream
response, a 128 KiB request limit and an 8 MiB response limit. Oversized incoming
bodies return 413 before forwarding. Worker-generated errors include security headers. Upstream failures return JSON `502`; a failed write response
means an unknown result, so retry the same operation UUID and snapshot. Invalid
ingress configuration returns `503`. API responses are never cached. The Worker
does not follow upstream redirects; redirects to the same upstream are rewritten
to the frontend origin and redirects elsewhere are rejected.

For a Compose staging deployment, apply migrations before starting the release:

```sh
docker compose --env-file .env.production -f compose.production.yaml build
docker compose --env-file .env.production -f compose.production.yaml run --rm migrate
docker compose --env-file .env.production -f compose.production.yaml up -d api bot
```

This Compose file uses an external PostgreSQL database and publishes the API
only on loopback. Provide a trusted HTTPS reverse proxy for browser access.
Run only one bot process; its database lease prevents competing pollers.
On Railway, run `env DB_STATEMENT_TIMEOUT_MS=60000 DB_LOCK_TIMEOUT_MS=10000 /app/entrypoint.sh python manage.py migrate --noinput`
once per release before API and bot replacement; do not attach independent migration steps to both services. Use
the explicit start commands in the API and bot manifests; both invoke `/app/entrypoint.sh`.
Configure `/api/health/` as the API process probe path and at least a 45-second
graceful shutdown window. The Compose API healthcheck uses the same route.

Provision each operator interactively; passwords are not command arguments:

```sh
docker compose --env-file .env.production -f compose.production.yaml exec api python manage.py create_crm_operator alice
docker compose --env-file .env.production -f compose.production.yaml exec api python manage.py revoke_crm_operator alice
```

Use one account per operator. Revocation disables that account and preserves
historical operation attribution. Do not create staff or superuser operators.

## Deployment acceptance

Check the actual public domain, not only unit tests:

1. Confirm HTTPS has no redirect loop and cookies are secure, host-only and HttpOnly.
2. Complete login, session discovery, CRUD and logout through the Worker.
3. Confirm unsafe requests without CSRF fail and API replies are JSON with `no-store`.
4. Confirm direct backend CRM requests and forged ingress headers fail.
5. Confirm separate client sources receive separate login limits.
6. Confirm Admin is inaccessible from the public direct URL.
7. Verify the supplied database certificate and reject an incorrect hostname or CA.
8. Verify static security headers on HTML, assets and SPA fallback responses.
9. Restart API and bot, test the queue recovery and test a second bot contender.
10. Complete the restore and rollback checks in [operations.md](operations.md).

Real proxy addresses, certificates, credentials, account authorization, MFA and
hosting restart policies cannot be established from repository configuration.
They remain release acceptance requirements.
