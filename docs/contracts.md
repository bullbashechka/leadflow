# Implementation contracts

Interface reference for stages 2–6 and 9. Delivery status is recorded in TASKS.md;
endpoint and transport implementation belongs to its assigned stage.

[PRD](../PRD.md) owns product behavior and acceptance criteria.
[TASKS](../TASKS.md) owns the stack, delivery order and verified progress.
This document owns the technical interfaces below. All examples use fictitious data.

## Deployment and trust boundary

| Component | Target | Address or region |
| --- | --- | --- |
| React SPA and API proxy | Cloudflare Workers Static Assets | `https://<worker>.<account>.workers.dev` |
| Django API | Railway, one always-on service | `https://<api-service>.up.railway.app`; Amsterdam `europe-west4-drams3a` |
| Telegram polling bot | Railway, one always-on service | No public HTTP endpoint; same region as API |
| PostgreSQL | Railway, persistent volume | Same production project, environment and Amsterdam region; private network, direct connection, verified TLS |

Addresses are templates, not deployed links. Record the actual addresses during stage 9.
Use the actual Railway PostgreSQL private connection details. Do not expose a public
database endpoint by default. Configure a trusted database certificate that matches
the actual connection hostname. Use `sslmode=verify-full` and its root certificate;
the default Railway connection URL alone does not satisfy this requirement. Verify
this configuration during stage 8 before treating the connection as production-ready.
Do not enable Railway serverless sleep for the API or bot. Stop the local polling process
before a deployment uses the same bot token. Use provider-generated frontend and API
addresses for the first release. Keep one production environment; local checks and
external acceptance replace a permanent cloud staging environment. Releases are
started manually through CLI from a verified GitHub revision, with no GitHub autodeploy.
Production backups are outside the HR assignment scope recorded in
[TASKS](../TASKS.md). The current recovery status and reference procedure are in
[operations](operations.md#production-backup-and-restore-reference).

The browser uses relative `/api/` URLs. A Worker proxies `/api/*` to one fixed Railway
origin. Static assets and SPA navigation use the ASSETS binding. Configure
`assets.run_worker_first` for `/api/*` so an API failure never returns the SPA shell.
The Worker must:

- Preserve the path, query, method and body. Never accept an upstream URL from user input.
- Forward `Cookie`, `Origin`, `Referer` and `X-CSRFToken`. Set the upstream Host from the
  fixed Railway URL. Do not use a client-supplied forwarded host for Django routing.
- Preserve status, JSON body and each separate `Set-Cookie` header. Use manual redirect
  handling; never forward credentials to another destination.
- Bypass cache for API fetches. Set `Cache-Control: no-store` on API responses.
- Return the error envelope below with HTTP 502 for upstream failures, including timeouts.
  A failed create response has an unknown outcome; the Worker must not claim rollback.

Django allows the actual Railway hostname and trusts the exact HTTPS frontend origin for
CSRF. Cookie Domain remains unset: proxied cookies belong to the public frontend host.
No browser-to-Railway CORS access is required. Keep Django authentication and CSRF checks
active on the Railway address too. A direct request must not bypass access checks.
Trust HTTPS forwarding only from Railway's documented proxy boundary, not from arbitrary
incoming headers. Production API ingress also requires a constant-time checked shared
server secret supplied by the Worker; client-supplied ingress and forwarding headers are
stripped and replaced at the Worker. Only authenticated ingress supplies the verified
client address used for CRM login limits. Health exposes no customer data and remains
available to the hosting probe. The Worker does not proxy Django Admin; technical
administration uses the Railway address, an allowed source network and a separate login.

Local development keeps the existing Vite `/api` proxy. The local frontend origin must
be in `DJANGO_CSRF_TRUSTED_ORIGINS` when the login API is implemented. Production cookies
are secure; local HTTP settings remain local only.

PostgreSQL owns leads, tags, sessions, bot drafts and submission receipts. Neither process
stores durable data on the API or bot service's ephemeral filesystem. The database
uses a persistent Railway volume. Migrations are an explicit release
step before API and bot start; neither process independently applies them at startup.

## Module and secret boundaries

| Module | Responsibility |
| --- | --- |
| `backend/leadflow/crm/` | Models, shared field validation, lead queries, transactional creation and submission receipts |
| `backend/leadflow/crm/api/` | Authenticated session, tag and lead endpoints; request and error mapping |
| `backend/leadflow/bot/` | Persistent dialogue state, question binding, Telegram transport and polling |
| `backend/leadflow/users/` | Existing Django user model and technical administration |
| `frontend/src/` | API client, view state, form snapshots and display of device-local dates |

Put synchronous domain operations in `crm/services.py` and field rules in
`crm/validation.py`. API and bot call these operations; neither copies persistence rules.
Keep draft transitions in `bot/services.py`. A submission operation in the CRM layer
owns the transaction that also completes a bot draft. Async bot handlers call synchronous
operations through `sync_to_async(thread_sensitive=True)` with database connection cleanup
inside the synchronous boundary. Do not hold a database transaction during Telegram calls.

This table records supported environment keys and explicitly planned deployment settings.
Do not describe a planned value as currently supported.

| Variable | Consumer | Rule |
| --- | --- | --- |
| `BOT_TOKEN` | Bot only | Existing setting; inject at runtime, never include in logs or builds |
| `CRM_AUTH_MODE` | API | `individual` in production; explicit `demo` is supported only locally and in tests |
| `CRM_DEMO_PASSWORD_HASH` | Local API | Django encoded password hash; required only in demo mode |
| `DJANGO_SECRET_KEY` | API and bot | Existing setting; stable across restarts |
| `DATABASE_URL` | API and bot | Existing setting; same Railway PostgreSQL, private hostname and verified TLS options |
| `DJANGO_ALLOWED_HOSTS` | API | Existing setting; exact deployment hosts |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | API | Existing setting; exact frontend origin, no wildcard |
| `VITE_TELEGRAM_BOT_URL` | Frontend build | Public `https://t.me/<bot_username>` link; never pass the bot token |
| `API_ORIGIN` | Worker | Fixed HTTPS Railway origin, server-side configuration |
| `INGRESS_SHARED_SECRET` | API and Worker | Server-only shared ingress secret; never include in the static frontend |
| `DJANGO_TRUSTED_PROXY_CIDRS` | API | Verified immediate proxy networks; never infer them from client headers |
| `DJANGO_ADMIN_NETWORK_ALLOWLIST` | API | Permitted administrator source networks; empty means closed in production |

In local demo mode, an unset or malformed password hash must disable login with a
configuration error. It must never enable an empty password. Generate the encoded hash
with Django's configured password hasher and supply it through the ignored local `.env`.
The bot does not need the demo hash. The only bot-related frontend value is the public
Telegram URL. The frontend receives no database credentials, password hash, bot token or
Django secret through Vite build variables.

## Session and CSRF contract

Use database-backed Django sessions. Production uses individual operator accounts in the
existing Django user model. Permit only active non-staff, non-superuser accounts with a
usable password; no public registration is exposed. The first HR demonstration provisions one individual `demo` account in the shared CRM
workspace. Operator permissions remain identical. An Admin login alone never grants CRM access.

Explicit local/test demo mode checks `CRM_DEMO_PASSWORD_HASH` with Django's password
checker and logs in the internal non-staff principal with an unusable normal password.
Production rejects demo mode. Keep technical Admin authentication separate.

On a successful login, rotate the session key and CSRF token. Store `expires_at` as
login time plus 48 hours in UTC and set the session expiry to that absolute datetime.
Do not refresh it on requests. Use persistent cookies, with `HttpOnly`, `Secure` and
`SameSite=Lax` in production. Cookie Domain remains unset. Cookie Path is `/`.

Every CRM access check requires the correct authentication mode, access marker, unexpired
UTC deadline, permitted active user, current password fingerprint and `auth_revision`. A password change
or deactivation revokes that operator's sessions without revoking other operators.
Each saved password, active, staff or superuser change advances the user revision.
Security-field QuerySet updates also advance it atomically. Restoring an earlier value
never restores a session. Operator revocation advances the revision even when repeated.
Changing the authentication mode invalidates old sessions. In demo mode, changing or
removing the configured hash revokes demo sessions at their next server check.

Protect login and logout explicitly with Django CSRF even for anonymous requests.
DRF SessionAuthentication alone does not protect an anonymous login endpoint.
The session GET response supplies a masked token from Django `get_token()` because the
CSRF cookie is HttpOnly. Send it as `X-CSRFToken` with every POST. Send cookies with
same-origin requests. Obtain the rotated token from the successful login response.
Never return session keys in JSON. All session and CRM responses use `no-store`.

Logout flushes the current server session. Other browser sessions remain valid.
Hide CRM immediately when logout is requested, including while offline. Persist only
a random pending-logout marker in localStorage and a host-only 48-hour cookie
(`Path=/`, `SameSite=Lax`, `Secure` on HTTPS). Either channel blocks access after reload and blocks
new login until logout is confirmed. Recheck session state and complete pending logout
on reconnect, focus and the active-tab five-second poll. If discovery confirms anonymous
access, logout is already complete. Otherwise use its current CSRF token to POST logout.
Clear both markers only after confirmation. Probe storage before restoring access
or starting login; if both channels are unavailable, keep access closed. A lost logout response keeps the marker.
Broadcast logout requests, confirmed logout and login between same-origin tabs using
BroadcastChannel; use the storage event fallback when unavailable. Events contain only
type and random identifier, no form data, token or cookie.
Close access behind a re-authentication dialog while retaining form values, submission
UUID and the frozen request snapshot in memory. On focus and on authentication failures,
recheck the session so missed notifications do not leave stale access visible.
After login in another tab, refresh the session and CSRF token before resuming a mutation.
Never automatically submit retained form data on login.

Serialize session discovery, login and logout across tabs using Web Locks. Support current
Chrome, Firefox and Safari on HTTPS (localhost HTTP is permitted for development). An
unsupported browser stays closed with an explanatory message. Ignore stale session and
data replies after access changes. Protected children remain mounted after their first
successful login but are hidden and inert while locked, including to assistive technology.
CRM portals are contained in that protected subtree; outstanding confirmation dialogs are
destroyed on lock and are never restored or submitted automatically after login.
Form state and tokens stay in memory, never in localStorage.

The session response includes `server_time`. Derive remaining lifetime from server UTC
times, subtract request elapsed time conservatively and use a monotonic client timer.
Check expiry before operations and when a tab returns. Previously validated access may
remain visible and editable offline until its known deadline; mutations require connectivity.
Initial discovery failure never opens access. Authentication failures close access;
CSRF failures refresh the token without replaying the rejected operation.

Limit CRM login per sixty-second window to ten attempts per verified client source,
twenty per normalized account and sixty globally. Lock keyed digest counters in sorted
order within one transaction; spend all buckets only when all permit the attempt.
Unknown accounts use the same limits. Store no raw source addresses or account names
in counters. Return 429 `rate_limited`
with `Retry-After`. Do not trust client-supplied forwarding headers. Verify and configure
the trusted cloud proxy chain at deployment. Authenticated Worker ingress supplies a
validated client address; unverified forwarding never selects a login bucket. Admin
attempts use a separate bucket and production Admin routes require an allowed network.
Run `cleanup_crm_auth` periodically to delete expired sessions and counters older than a day.

The API returns HTTP 401 for missing or expired CRM access. Adapt DRF's session
authentication/exception mapping to this contract; its default anonymous response can be
403. Reserve HTTP 403 for CSRF or permission rejection. Django's CSRF failure handler
must return the same JSON error envelope as the API. Authentication remains server-side.

## Public API

Session, tag and lead routes are implemented. The lead list keeps offset links for
compatibility and supports stable arrival-sequence cursors across edits and deletion.
All routes have a trailing slash. Bodies and responses are JSON.
Only session discovery, login and the existing minimal `/api/health/` route are public.
Logout is CSRF-protected and can also clear an already expired session. All lead and tag
routes require CRM access. Return errors as JSON, not redirects to Admin or HTML pages.

### Session routes

`GET /api/auth/session/` returns HTTP 200 for either state:

```json
{
  "authenticated": false,
  "auth_mode": "individual",
  "expires_at": null,
  "server_time": "2026-10-02T12:00:00+00:00",
  "csrf_token": "<masked-token>"
}
```

`POST /api/auth/login/` in individual mode requires exactly these fields:

```json
{"username": "<operator-login>", "password": "<operator-password>"}
```

Explicit local/test demo mode accepts exactly one field:

```json
{"password": "<demo-password>"}
```

Its HTTP 200 response has the same session shape with `authenticated: true`, the fixed
UTC expiry and a fresh CSRF token. Invalid credentials return 401 `invalid_password`.
Missing or invalid input returns 400 `validation_error`; an unavailable login
configuration returns 503 `configuration_error`. A repeated login while already
authenticated returns the current state without extending its expiry.

`POST /api/auth/logout/` accepts `{}` and returns HTTP 204 with no body. Repeated logout
is harmless. A CSRF failure returns 403 without claiming logout succeeded. If a logout
response is lost, recheck the session before displaying success.

### Tags and leads

Tag identifiers are positive integers. A tag has `id`, `name`, `is_system` and, on the
list endpoint, `lead_count`.
`GET /api/tags/` returns HTTP 200:

```json
{
  "results": [
    {"id": 1, "name": "Сайт", "is_system": true, "lead_count": 12},
    {"id": 2, "name": "Реклама", "is_system": true, "lead_count": 4},
    {"id": 3, "name": "Автоматизация", "is_system": true, "lead_count": 1},
    {"id": 4, "name": "Другое", "is_system": true, "lead_count": 0}
  ]
}
```

IDs in examples are illustrative. Seed and address system tags by stable codes `website`,
`advertising`, `automation` and `other`, not by an assumed primary key or translated name.
The direction-to-code mapping is fixed. Direction buttons carry codes; the lead receives
the corresponding persisted tag ID.

Lead IDs are UUID strings. `source` is `manual` or `telegram_bot`. `status` is `new`,
`in_progress` or `closed`; creation always sets `new`. `created_at` is an immutable UTC
ISO 8601 datetime. `version` is a positive integer incremented when editable lead values,
status, or assigned tags change. `note` is an optional string, empty when absent, capped at 5000
characters. `is_demo` marks fictional review data. `arrival_sequence` is an immutable,
positive, unique sequence assigned under a singleton database lock when a lead is created.
Use `Intl.DateTimeFormat` with the device timezone and an explicit timezone label for
display. Do not turn the displayed local date back into the stored creation timestamp.

`GET /api/leads/{id}/` returns HTTP 200 with the lead representation:

```json
{
  "id": "7312e490-ae04-4f19-9f18-41d7d125db38",
  "name": "Тестовый клиент",
  "contacts": [{"type": "email", "value": "client@example.com"}],
  "request": "Нужен сайт агентства",
  "note": "Связаться после обеда",
  "source": "manual",
  "status": "new",
  "version": 1,
  "is_demo": false,
  "arrival_sequence": 7312,
  "created_at": "2026-10-01T12:00:00Z",
  "tags": [{"id": 1, "name": "Сайт", "is_system": true}]
}
```

An unknown lead returns 404 `not_found`. `GET /api/leads/` supports:

| Parameter | Contract |
| --- | --- |
| `tag_id` | Optional positive integer; one existing tag. Unknown tag returns 400 |
| `status` | Optional `new`, `in_progress` or `closed` |
| `q` | Optional search text, max 200 characters. Every whitespace-separated word must match at least one name, contact or request field. Matching is case-insensitive; phone-shaped terms match phone digits without display formatting. Digits inside email terms do not match unrelated phones |
| `limit` | Integer 1–100; default 50 |
| `offset` | Integer 0–100000; default 0 |
| `before_id` | Optional lead UUID in the active result set. Return rows with a lower `arrival_sequence`. Do not combine with a nonzero `offset` |
| `before_sequence` | Optional positive sequence cursor. Return rows with a lower `arrival_sequence`; use it when the previous lead may have been deleted |
| `since_sequence` | Optional integer >= 0; calculate new matching rows after this sequence |
| `exclude_id` | Repeatable UUID; exclude matching leads from `new_count` only, max 100 |

Tag, status and search conditions combine with AND. Search terms combine with AND; for each
term, matching across name, contacts and request combines with OR. Sort by
`arrival_sequence DESC, id DESC`. Invalid query parameters return 400 `validation_error`;
repeated single-value parameters and unsupported parameters are also invalid. A missing
anchor, or one outside the selected filters, returns a field error for `before_id`. A
`before_sequence` cursor remains usable after its lead is deleted. The list returns the
lead objects described above:

```json
{
  "count": 0,
  "next": null,
  "previous": null,
  "new_count": 0,
  "latest_sequence": 7312,
  "results": []
}
```

`count` is the total matching the filters before pagination. `new_count` counts matching
leads newer than `since_sequence`, less any `exclude_id` values. `latest_sequence` is the
global sequence high-water mark, including deleted leads, captured before reading the
page. Results, total and new counts include only arrivals at or below that watermark so
an arrival between queries remains discoverable on the next poll. For offset requests, `next` and
`previous` are relative `/api/leads/` URLs with the same filters and pagination parameters,
or null. A cursor response uses `before_sequence` in `next`; `previous` is null because the
client retains earlier rows while appending. The offset contract remains supported.
Do not cache authenticated query results at the proxy.

`frontend/src/leadList.ts` owns list polling and reconciliation. Poll every 5 seconds
only while the tab is visible and authenticated, with available access. Refresh immediately
on focus, visibility, online, pageshow and access recovery. Abort pending reads when polling
is disabled or the filter changes. Serialize head and older-page requests; coalesce pending
polls. Late aborted responses cannot change the state. Keep form state independent from
fetched list state. The product's ten-second target requires a visible tab, a working
connection and an available API; browser suspension or discard cannot meet this target.

`new_count` is independent from lead status and is not an unread flag. Exclude only confirmed
own-create UUID receipts for this tab. Reset the baseline on filter change or successful
presentation at the list beginning. Each tab owns its baseline, filter and scroll anchor.
Do not synchronize them through the authentication bus. Stage 7 lead edits and deletions
reconcile their confirmed result into the visible list and refresh the server count.

Regular background polls read at most two windows of 100 rows: the head and, when needed,
the currently visible older window. Merge versions and filtered membership even when
the total count is unchanged. Retain loaded rows outside those windows and the viewport
anchor. Refresh older rows when they become visible instead of polling all loaded history.
When explicitly accepting arrivals or reconciling a local mutation, read the required
prefix through its cursor and retain all loaded pages without duplicate UUIDs. Never
advance the accepted watermark while an incoming prefix is incomplete. Completeness
counts exclude exactly the own receipts excluded in the request, not every new row.
Recheck presentation eligibility before committing a result. Forms and cards continue
receiving background counts without arrival notifications or changes to their contents.

Failed head reads retain rows and the last successful timestamp until a head read succeeds.
An older-page success does not clear that warning. A failed filter change retains the
selected filter, hides prior results and retries the selected query. Confirmed mutations
cancel older reads; stale mutation replies update their own list record but never replace
another selected detail card. Keep the immutable operation target through reauthentication.

### Lead mutations

All mutation endpoints require the authenticated CRM session, CSRF protection and a fresh
`operation_id` UUID. The client retains that UUID and the exact request after an unknown
network outcome. Reusing it with another operation or payload returns 409
`operation_conflict`. Attributed receipts retain the original operator; another operator
cannot replay an attributed operation or obtain its result. Historical and bot actors
may be null. Mutations lock the lead row and compare `expected_version`; a stale
request returns 409 `version_conflict` with the current `current_lead` representation.
Missing leads return 404 `not_found`; known deleted leads return 410 `lead_deleted`.

`PUT /api/leads/{id}/` edits all user-editable lead values as one unit:

```json
{
  "operation_id": "6eac13d2-8603-42d2-8b85-5cfe9d14ca51",
  "expected_version": 1,
  "name": "Тестовый клиент",
  "contacts": ["client@example.com"],
  "request": "Нужен сайт агентства",
  "note": "Связаться после обеда",
  "tag_ids": [1]
}
```

The response is `{ "operation_id", "replayed", "applied_version", "lead" }`. Name,
contacts and request use shared validation; `tag_ids` must contain distinct existing IDs.
Source, creation time, demo flag and arrival sequence are server-owned. An edit of a known
deleted lead never recreates it.

`PATCH /api/leads/{id}/status/` accepts `operation_id`, `expected_version` and one status.
Status is saved independently from the detail edit form. It uses the same version and
idempotency rules as `PUT`.

`DELETE /api/leads/{id}/` accepts `operation_id` and `expected_version`. Success returns
`{ "lead_id", "deleted": true, "replayed" }`. The row, contacts and tag relations are
deleted in one transaction. A submission receipt is retained as a tombstone: its payload
is cleared, its request hash and deleted lead UUID remain. Replaying the original submission
returns 410 `submission_deleted`, so a deleted lead cannot return through a bot or manual
retry. The tag dictionary is unaffected.

### Tag mutations

`POST /api/tags/` accepts `{ "operation_id": "<uuid>", "name": "Тег" }`. Trim surrounding
whitespace; require a nonempty name up to 40 characters; compare names without case. A
new tag returns 201 with `{ "tag", "created": true, "replayed": false }`. A duplicate
returns 200 with the existing tag and `created: false`; it does not assign that tag to a
lead. A committed create replay returns the original tag with `replayed: true`.

`DELETE /api/tags/{id}/` accepts `{ "operation_id": "<uuid>" }`. It removes a custom tag
and all of its lead assignments atomically, then returns `{ "tag_id", "deleted": true,
"replayed", "affected_leads" }`. It never deletes a lead. It increments each affected
lead's version so an open edit cannot silently reassign the deleted tag. Deleting one of
the four system tags returns 409 `protected_tag`.

### Manual creation

`POST /api/leads/` requires `submission_id`, `name`, `contacts` and `request`.
`contacts` is a nonempty array of strings. Responses expose ordered `{type, value}`
contacts without a preferred contact.
`tag_ids` is optional and defaults to an empty array. Server-owned fields are not writable;
unknown fields return 400 rather than being silently ignored.

```json
{
  "submission_id": "513c476d-64c4-4879-b964-fc4d1a41d65e",
  "name": "Тестовый клиент",
  "contacts": ["client@example.com"],
  "request": "Нужен сайт агентства",
  "tag_ids": [1]
}
```

`submission_id` must be a UUID. `tag_ids` contains distinct existing integer IDs.
Apply PRD field rules through shared server validation. Do not truncate input. Preserve
accepted text; use whitespace checks to reject blank fields. Return field errors for
invalid contacts, lengths, missing fields and invalid tags. Source, initial status and
creation time come from the server.

First success returns 201 with the lead object. An identical successful replay returns
200 with that same lead ID. A different payload for an already committed submission
returns 409 `submission_conflict` without modifying the lead.


### Shared contact rules

Shared text validation rejects NUL characters with a field error before persistence.
This applies to names, requests and contact strings in manual and bot operations.

Accept 1 or more contact strings, each at most 254 characters including surrounding
whitespace. Preserve accepted input; trim only for validation and duplicate keys.
Phones require an initial `+`, ASCII digits, and optional spaces, parentheses and hyphens.
After removing separators, require 7–15 digits and a nonzero first digit. This is a format
check, not a country-code or reachability lookup. Email uses Django EmailValidator with
an empty domain allowlist. Telegram accepts `@username`, or an HTTP(S) or bare `t.me` /
`telegram.me` profile link with one username path component and an optional trailing slash.
No credentials, port, query, fragment, post or invitation links are allowed. Usernames use
5–32 ASCII letters, digits or underscores, beginning with a letter.

Duplicate keys: phone digits with `+`; casefolded Telegram username; email local part
unchanged plus casefolded domain. Check every input before deduplication, preserving the
first spelling and order. Errors use `contacts.<zero-based-index>`; list errors use
`contacts`. No `contact` compatibility field is supported: the endpoints do not exist yet.

### Error envelope

```json
{
  "code": "validation_error",
  "message": "Проверьте заполнение формы.",
  "field_errors": {"contacts.0": ["Укажите телефон, email или Telegram-контакт."]}
}
```

`code` is stable machine-readable text; `message` and field messages are Russian display
text. `field_errors` is always an object, empty for non-field errors. Never expose provider
responses, stack traces, database details, credentials or customer data in error messages.

| HTTP | Code | Client action |
| --- | --- | --- |
| 400 | `validation_error` | Display field/query errors; the rejected request created no lead |
| 401 | `invalid_password`, `authentication_required` | Correct password or re-authenticate; retain form and operation |
| 403 | `csrf_failed`, `permission_denied` | Refresh session/token or explain rejection; do not create a new operation |
| 404 | `not_found` | Explain that the requested lead or tag is unavailable |
| 409 | `submission_conflict`, `version_conflict`, `operation_conflict`, `protected_tag` | Keep the form and operation; show current lead values for a version conflict, or explain the rejected operation |
| 410 | `lead_deleted`, `submission_deleted`, `tag_deleted` | Explain that the record was deleted; do not recreate it from a retry |
| 429 | `rate_limited` | Keep input and wait for the `Retry-After` interval before login |
| 500/503 | `save_failed`, `service_unavailable`, `configuration_error` | Preserve input; retry the same operation when applicable |
| 502 | `upstream_unavailable` | Treat a create outcome as unknown; retry the frozen request |

A network error, timeout, aborted create request or malformed success response also has
an unknown outcome. Only an explicit validation/access rejection or a server error that
confirms transaction rollback permits editing the same form. Treat other 5xx outcomes
conservatively as unknown. Do not infer failure to persist from an HTTP status alone.

## Submission identity and persistence

Create one UUID for each new manual form and each new bot draft. This UUID identifies a
submission, not a contact. Never deduplicate distinct submissions by phone, username,
email, name or request text. Re-authentication and retries keep the UUID unchanged.

Persist a submission receipt with a unique UUID, channel, owner (Telegram user ID for
bot submissions), validated payload and resulting lead ID. A receipt is committed in the
same transaction as its lead. For a bot submission, remove the active draft and update the last receipt reference
in that transaction too. A completed submission is represented by its durable receipt. Keep receipts across restarts and draft replacement so old confirmations can resolve
the result. Cancelled, unsubmitted draft data is deleted; it has no success receipt.

Use the database unique constraint and a transaction to arbitrate concurrent requests.
The loser reads the committed receipt and returns the result. If the competing transaction
rolls back, the retry may create the lead. Do not use a process-local lock as the only
duplicate protection. Compare text fields and the ordered original contact strings exactly; compare tag IDs
as an unordered set. Store the original validated request separately from deduplicated
contact rows.
Check a committed receipt before validating current tag availability on a replay.
Reject a UUID owned by a different channel or Telegram user without exposing its result.

Before sending a manual request, retain its immutable payload snapshot in memory. Disable
editing and in-app form closure while pending or unknown. The check-and-retry action sends
that same POST, not a new operation. On 201/200 open the returned card and update list
data. On an explicit no-write rejection, unlock the form. A committed payload conflict
does not unlock editing to overwrite the saved lead. Login does not resend automatically.
Reload recovery is outside P0; no form or customer data is stored in localStorage.

Deleting a lead preserves submission identity: replaying a known submission cannot
resurrect a deleted lead. The API retains a tombstone with the original request hash and
deleted lead UUID while clearing the saved payload. Deletion is exposed only through the
authenticated, version-checked CRM mutation endpoint.

## Bot dialogue operations

The bot handles intake in private chats. Use one persistent active draft per Telegram
user. Persist `submission_id`, values, current step, revision, review/edit return state,
question message ID/date, current inline-control message IDs, pending review inputs,
correction state and submission state. Store selected services in `values.directions` as
stable tag codes. Keep the last completed receipt reference and latest accepted source
message IDs in BotUser. Lock this row for every draft mutation, confirmation and consistent
dialogue read. An active draft has submission state `collecting`; a successful submission
removes it and retains the receipt. A new draft uses a new UUID. Valid field updates
increment the revision.

Store user inputs in DraftInput rows. A row identifies a name, contact or request part and
keeps its position, original Telegram message ID, accepted text, optional pending text and
active/editable state. This lets an `edited_message` update find its value without matching
text or changing a later draft. Keep unconfirmed review messages in the draft's pending
input list until the user adds or discards them. Old scalar `values.direction` is converted
to a one-element `directions` list during migration.

Internal operations receive the Telegram user/chat identity and event identity from the
transport layer. They return structured outcomes; handlers map them to Telegram messages.
Keep product behavior and user-visible copy in [PRD.md](../PRD.md#сбор-заявки-telegram-ботом).

| Operation | Input | Result |
| --- | --- | --- |
| `get_dialogue` | User identity | Active draft, last completed receipt or empty state |
| `start_draft` | User identity, explicit restart flag | New UUID; restart removes the unsubmitted previous draft |
| `bind_question` | User identity, draft UUID/revision, successful outgoing Message ID/date | Persist the current question binding |
| `set_field` | User identity, draft UUID/revision, field, value, incoming event binding | Validated field and next step, or error with no transition |
| `continue_directions` | User identity, draft UUID/revision | Require one or more selected directions and advance to request |
| `append_request_part` | User identity, draft UUID/revision, text and source-message identity | Append a paragraph within the full request limit, or retain an over-limit part for correction |
| `continue_request` | User identity, draft UUID/revision | Require a non-empty valid request and advance to name |
| `edit_input_message` | User identity, draft UUID/revision, source-message ID and new text | Update only the linked current-draft value, or leave accepted data unchanged |
| `begin_edit` | User identity, draft UUID/revision, selected field, optional contact index | Requested field; retain other values and return to review after correction |
| `remove_contact` | User identity, draft UUID/revision, contact index | Remove one contact; require replacement if none remain |
| `remove_request_part` | User identity, draft UUID/revision, request-part position | Remove one accepted request paragraph |
| `append_review_input` | User identity, draft UUID/revision, text and source-message ID | Store a new review message without applying it |
| `decide_review_input` | User identity, draft UUID/revision, add/discard choice | Append pending text or discard it; only then enable confirmation |
| `choose_username` | User identity, draft UUID/revision | Add the username captured from Telegram as an explicit contact |
| `cancel_draft` | User identity, draft UUID/revision | Delete the active unsubmitted draft, or stale/submitted outcome |
| `confirm_draft` | User identity, draft UUID/revision | Shared transactional creation result or an earlier success receipt |

The normal steps are direction → request → name → contacts → `contact_choice` → review.
Direction buttons toggle membership in `values.directions`; require at least one code
before continuing. After each accepted contact, enter `contact_choice` and offer add or
continue. Offer the user's Telegram username only as an explicit contact action when one is
available. Review can correct or remove an individual contact and remove an individual
request paragraph; confirmation requires at least one valid contact and one direction.
Drafts have no expiry. A populated draft requires explicit confirmation before cancel or
restart; an empty draft can be removed or replaced immediately. `/start` with an active
draft shows a short summary and resume/restart/cancel actions. A clean `/start` creates the
draft and displays directions immediately. With no draft and a completed receipt, acknowledge
the last submission and offer a new one.

Accept multiple text messages for the request step. Persist each as a separate paragraph
and check the total, including paragraph separators, against the 2000-character limit. Keep
the initial question message ID/date while the user adds parts. Queue each progress status
as a new `send` with its inline keyboard and `bind_question=false`. Track the latest
delivered progress message in `active_control_ids`; remove earlier controls without changing
their text. Strip superseded inline keyboards from pending outbox operations so delayed
delivery cannot publish obsolete controls. Explicit replies may target the initial question
or the latest active progress message, only during ordinary request collection.
If a part exceeds the total limit, retain it as pending, block continuation and allow the
user to edit its source message or discard it. Explicit review edits can replace the entire
request or append a paragraph with the same total-length check.

On any unprompted text at review, persist a pending input and show Add to request / Do not
add. Do not allow confirmation while pending inputs exist. An edited pending message updates
that candidate. An `edited_message` for a DraftInput updates only its linked value in the
current draft. Once submitted, do not update the lead; if the edited source belongs to the
latest accepted submission, tell the user that the submitted lead is unchanged. Telegram
does not send regular-bot updates for message deletion, so deletion is available through
the review's remove-part action.

Message input requires the persisted question binding except for messages handled as
explicit pending review text or native edits. Accept an explicit reply only when its
`reply_to_message.message_id` matches the current question in the same chat, or the latest
active progress message during ordinary request collection. For unbound
text or a contact-button response, require a message date strictly later than the bound
question date and the current expected input type. Earlier dates, stale explicit replies and
ambiguous same-second unbound messages do not advance the draft. Transport must never use
the trusted internal `event=None` shortcut for incoming messages. Input errors remain on
the current step. Persist every mutation before acknowledging it.

At each transition, queue removal of inline keyboards tracked for the prior step. When a
direction is selected, edit the same direction menu to show its new selection and carry the
new revision. When a request part is added, send the new progress keyboard below the answer
and remove earlier controls. Verify sender, draft UUID and revision for every callback. Clear a tapped stale
keyboard when possible; callbacks from untracked legacy messages still fail the server-side
revision check. Telegram edit failures do not make stale callbacks valid. Send the next
question as a new message only after its field transaction succeeds. Never replace an
earlier input question with the next question. Bind it only after Telegram confirms
delivery. If delivery/binding fails, keep the draft resumable and resend the current question.

Send input prompts, back navigation to input, and field-correction prompts as new messages.
Send review as a new message after a field correction or new pending review text. Reuse a
review message only for explicit menu navigation and button actions within review. Direction
selection updates its existing menu. Preserve previous message text and remove obsolete
keyboards. Do not assume Telegram message IDs are monotonic.

For the contact step, send one question with a resized, one-time `ReplyKeyboardMarkup`
containing the “Отправить мой номер” button (`request_contact=true`) and an explicit username
choice when available. Explain manual contact input in the same question and bind the
delivered message as the question. Decode reply keyboard markup before standalone ForceReply
so its buttons are retained. Do not send a separate step-actions message.

Accept a contact-button response only from the expected user (`contact.user_id` matches the
sender); manual text accepts all PRD contact formats. Remove the contact keyboard after an
accepted contact, on back navigation, and when showing the start or cancel/restart menu.
Show it again when adding or correcting a contact, resuming input, or retrying invalid input.
Split long summaries and contact lists into messages of at most 3900 characters, preferring
line boundaries. Attach the related actions to the last part, so Telegram's text limit
cannot hide the next dialogue action.
For a large review, split action rows into separate keyboards of at most 300 buttons;
keep cancel and confirmation together on the last keyboard. Each review row contains at
most two buttons. This avoids the truncation enforced by
[Telegram's markup implementation](https://github.com/tdlib/td/blob/master/td/telegram/ReplyMarkup.cpp).

Callbacks carry a compact action, UUID and revision, within Telegram's 64-byte limit.
Verify the sender, current draft and revision for every mutation. A submitted receipt can
answer an old confirmation without mutating a new draft. A cancelled/unknown/stale button
returns an explanation and the current step. Do not implement callbacks using only field
names or direction labels without draft identity.

Keep update handling sequential for the single polling process. In one PostgreSQL
transaction, store the processed update identity, domain transition, outbound-message
intentions and next offset. Telegram's next request acknowledges only updates below this
committed offset; a failed transaction leaves the update retryable. Set the offset to the
current update ID plus one, including replays; do not take the maximum across historical
sequences. [Telegram can choose a random update ID after a week without new events](https://core.telegram.org/bots/api#update).
On replay, advance the polling position without reapplying a field to a later step.
Request both `message`, `edited_message` and `callback_query` updates. A deleted private
message is not observable through this Bot API flow. Store each outbox operation as send,
edit text or edit inline markup, with the target Telegram message ID for edits; keep Telegram
calls outside the database transaction. Retain delivered Telegram message IDs so the bot
can update the request prompt, refresh current callbacks and remove prior keyboards.

After confirmation, persist the pending state, original revision and processed update
before creating the lead. Freeze edits, cancellation and new submissions while the result
is unknown. Retry the shared create operation with the same UUID and snapshot until a
receipt or a known validation result exists. Persist outgoing messages in an outbox and
send them outside database transactions. Retry network and flood-control failures. Preserve
message order per chat: a delayed pending message blocks later messages in that chat, while
other chats remain eligible. Delivery or permanent rejection releases the next message.
The delivery loop continues with other eligible chats after a network or server error.
A Telegram rate-limit response persists a bot-wide cooldown for the full `retry_after`.
All Telegram methods, including polling, pause until it expires; restarting the process
does not shorten it. Per-message retry deadlines also retain the full delay.
Both retry counters saturate at 32767, the maximum positive value for PostgreSQL smallint. Further
failures still persist their next retry time, and successful recovery remains available.
Drain pending confirmations and messages before long polling and after each processed update, so
recovery does not depend on another user message. Clear message text and markup after
delivery or a permanent rejection. Telegram may accept a message just before the process
stops; after restart the outbox can send it again. Duplicate prompts are acceptable; lead
creation remains idempotent. No delivery success may be claimed before it is known.

The polling process acquires a per-bot PostgreSQL session advisory lock on a dedicated
connection before any Telegram method. Check ownership before and after each method;
loss prevents applying returned updates. Close the connection on shutdown. A direct
connection or session pooler is required; transaction pooling is unsupported. This
prevents overlapping owners, not duplicate messages after an unknown send outcome.

Review retains at most ten pending messages and 4000 total pending characters. Reject
excess input without changing accepted data or silently truncating text. Source edits
also respect the aggregate bound. Intake uses the limits in [Resource limits and transport maintenance](#resource-limits-and-transport-maintenance)
and emits at most one rate notice per window. Replays do not spend the quota.
Keep these checks inside the existing durable update transaction.

`/start` takes precedence over ordinary input. With an active draft, offer resume/restart.
With no active draft and a completed receipt, acknowledge the last submission and offer a
new one. With neither, create a draft and show the direction choices immediately. A failed
success message never reopens a completed draft. Check the receipt before retrying uncertain
confirmation. Source edits while the draft is collecting may update their linked values;
edits after submission never update the saved lead.

## Implementation verification

These scenarios define the required verification for each implementation stage. Recorded
completion evidence belongs in [TASKS.md](../TASKS.md).

| Stage | Scenarios and required result |
| --- | --- |
| 2 | Restart preserves leads, tags, receipts and active drafts. Shared validation rejects PRD invalid input through manual creation and bot draft operations; API/transport adapters are checked at stages 4–5. Concurrent same-UUID creation commits one lead. A rollback permits retry; a lost response returns the original lead. A new UUID with equal data creates a separate lead. |
| 3 | Anonymous direct requests cannot read/write leads. Login and logout reject missing/wrong CSRF, including anonymous login. No secret appears in JSON/builds. Expiry is exactly 48 hours and does not slide. Reload/browser close does not shorten it. Logout invalidates only the current browser session and preserves another tab's form behind re-authentication. |
| 4 | Create with tags and without tags. Validation rejection preserves editable values. Uncertain result freezes fields and closure; retry opens one saved card. Re-authentication keeps UUID/snapshot and requires explicit retry. Timezone changes affect display only, including dates across midnight. |
| 5 | First `/start` shows multi-select directions. Multiple codes become multiple CRM tags. Request input accepts several paragraphs and sends progress below each answer, retaining only the latest controls and preserving rapid parts during delayed delivery. New steps, field-correction prompts and review after correction use new messages. Direction selection and review menu navigation update in place. Over-limit parts can be edited or removed. Name, phone/email/Telegram contacts, explicit username choice and multiple contacts work. Review accepts no unconfirmed text: Add/Do not add is required and candidates can be edited. Source-message edits change only the current draft; after submission the lead stays frozen. Prior controls are removed where Telegram permits and stale callbacks are rejected. Back, cancel/restart, resume, question binding and unknown-outcome retries preserve their state. |
| 6 | Real bot-to-CRM arrival within 10 seconds under working connectivity. Verify more new leads than one page, tied timestamps, active filters, focus refresh, unchanged scroll anchor/form and retry after a background failure. Stale responses cannot replace a newer filter. |
| 9 | Worker routes API failures to JSON, preserves cookies and multiple Set-Cookie headers, never caches customer data or follows credential-bearing redirects. Verify real origins, CSRF, TLS, restart persistence and the public desktop/phone journey. |

Use PostgreSQL integration tests for persistence and races, fake Telegram transport for
dialogue failures, and browser journeys for cross-tab sessions and the connected UI.
Run the existing checks from [README](../README.md#checks) at the implementation stages.
Stage 1 itself requires JSON-example parsing, local-link checks and a document/diff review.

## Documentation references

The decisions above were checked against official documentation through Context7:

- [Django transactions](https://docs.djangoproject.com/en/6.0/topics/db/transactions/),
  [constraints](https://docs.djangoproject.com/en/6.0/ref/models/constraints/),
  [data migrations](https://docs.djangoproject.com/en/6.0/topics/migrations/#data-migrations)
  and [validators](https://docs.djangoproject.com/en/6.0/ref/validators/).
- [Django sessions](https://docs.djangoproject.com/en/6.0/topics/http/sessions/) and
  [CSRF](https://docs.djangoproject.com/en/6.0/ref/csrf/).
- [Workers static asset routing](https://developers.cloudflare.com/workers/static-assets/routing/advanced/)
  and [Worker Request](https://developers.cloudflare.com/workers/runtime-apis/request/).
- [Railway regions](https://docs.railway.com/deployments/regions),
  [PostgreSQL](https://docs.railway.com/databases/postgresql),
  [private networking](https://docs.railway.com/networking/private-networking)
  and [volume backups](https://docs.railway.com/volumes/backups).
- [Telegram Bot API](https://core.telegram.org/bots/api): ForceReply, KeyboardButton,
  Message, callback data and getUpdates acknowledgement.


## Resource limits and transport maintenance

- Accept API bodies up to 128 KiB, measured in bytes before JSON and CSRF parsing.
  Larger bodies return JSON `413 request_too_large`, including streamed bodies without
  Content-Length. The Worker checks the same limit before forwarding. Its own 413,
  502 and 503 responses include CSP, HSTS, frame and MIME protection and `no-store`.
- Accept at most 100 raw contact entries per API payload and 20 unique validated
  contacts after deduplication. API and bot share the validator. Preserve legacy
  stored lists; reads, status changes and deletion do not truncate them. Full edits
  and draft submissions must reduce them to the current limit.
- Limit list offsets to 0–100000. Numeric IDs, sequence values and expected versions
  must be within JavaScript's safe integer range; expected versions start at 1.
  Reject out-of-range values with 400 before a database query.
- Runtime PostgreSQL connections use a 3-second connect timeout, an 8-second
  statement timeout and a 2-second lock timeout. Migrations use a separate profile;
  see [production](production.md#runtime-settings). Keep TLS verification enabled.
- `/api/health/` is a public process probe with no SQL. `/api/readiness/` checks the
  database and requires a valid CRM session and production ingress. Private
  `release_status` remains the operational aggregate check.
- Intake ignores group updates before creating user or transport history. Admit
  at most 60 private events per user per minute. Bound pending outbox operations to
  1024 globally per bot and 64 per chat. Capacity exhaustion rolls back the draft
  transition and its outbox writes, then schedules one deferred warning per window.
  Pending submission completion commits the lead and success reply together.
- Persist an outbound slot before a provider call: at most 10 outbox operations
  per second globally and 1 per second per chat, including failed attempts.
  Select eligible chats by their last delivery attempt, preserving FIFO within each
  chat. Polling and delivery run separately; persisted full Telegram retry_after
  blocks all Telegram calls. Restart does not bypass pacing or cooldown.
- Clean terminal ProcessedUpdate and outbox history older than seven days in batches
  of 1000, hourly and after startup. Retain events referenced by pending outboxes,
  pending submissions or deferred capacity warnings. Preserve business receipts and
  the stored polling offset. Active drafts have no automatic expiry.
- Keep at most 32 closed DraftInput records per field, with accepted text scrubbed.
  Preserve active inputs, inputs still open for native editing, and pending corrections.
  Retain the latest position so pruning cannot reuse a closed input identifier.

## Edit attempt integrity

Freeze the request UUID, payload, original values and submitted contact row indexes
at the first attempt. Unknown outcomes retry that exact request. Navigation and lead
deletion stay blocked until its result is known. Pending or unknown deletion also
blocks new edit saves and navigation until the delete operation is resolved. Known conflicts permit review; use
original values from the first attempt for three-way merge, even when the form was
clean. Map API contact indexes back to submitted rows after empty-row filtering.
Confirm dirty cancellation. Keep overlong pasted tag names and notes for correction;
do not truncate them. Ignore detail GET responses older than the currently displayed
lead version. Telegram links parse HTTP schemes case-insensitively and always use the
fixed `https://t.me/<username>` destination after validation.
