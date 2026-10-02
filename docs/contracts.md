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
| PostgreSQL | Supabase | Frankfurt `eu-central-1`; session pooler, port 5432, TLS |

Addresses are templates, not deployed links. Record the actual addresses during stage 9.
Use the connection details supplied by Supabase; do not construct a pooler hostname.
Use `sslmode=verify-full` and the supplied root certificate for database connections.
Do not enable Railway serverless sleep for the API or bot. Stop the local polling process
before a deployment uses the same bot token.

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
incoming headers. The Worker does not proxy Django Admin; technical administration uses
the Railway address and a separate technical login.

Local development keeps the existing Vite `/api` proxy. The local frontend origin must
be in `DJANGO_CSRF_TRUSTED_ORIGINS` when the login API is implemented. Production cookies
are secure; local HTTP settings remain local only.

PostgreSQL owns leads, tags, sessions, bot drafts and submission receipts. Neither process
stores durable data on Railway's local filesystem. Migrations are an explicit release
step before API and bot start; neither process independently applies them at startup.

## Module and secret boundaries

| Module | Responsibility |
| --- | --- |
| `backend/leadflow/crm/` | Models, shared field validation, lead queries, transactional creation and submission receipts |
| `backend/leadflow/crm/api/` (planned) | DRF serializers, session endpoints, permission checks and error mapping |
| `backend/leadflow/bot/` | Persistent dialogue state, question binding, Telegram transport and polling |
| `backend/leadflow/users/` | Existing Django user model and technical administration |
| `frontend/src/` | API client, view state, form snapshots and display of device-local dates |

Put synchronous domain operations in `crm/services.py` and field rules in
`crm/validation.py`. API and bot call these operations; neither copies persistence rules.
Keep draft transitions in `bot/services.py`. A submission operation in the CRM layer
owns the transaction that also completes a bot draft. Async bot handlers call synchronous
operations through `sync_to_async(thread_sensitive=True)` with database connection cleanup
inside the synchronous boundary. Do not hold a database transaction during Telegram calls.

The following environment contract is planned. Existing `.env.example` documents the
foundation keys; new keys must be wired into settings and examples at their owning stage.
Do not describe an unwired key as currently supported.

| Variable | Consumer | Rule |
| --- | --- | --- |
| `BOT_TOKEN` | Bot only | Existing setting; inject at runtime, never include in logs or builds |
| `CRM_DEMO_PASSWORD_HASH` | API | Django encoded password hash; required when stage 3 login is enabled |
| `DJANGO_SECRET_KEY` | API and bot | Existing setting; stable across restarts |
| `DATABASE_URL` | API and bot | Existing setting; same PostgreSQL, TLS options and pooler details |
| `DJANGO_ALLOWED_HOSTS` | API | Existing setting; exact deployment hosts |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | API | Existing setting; exact frontend origin, no wildcard |
| `API_ORIGIN` | Worker | Planned fixed HTTPS Railway origin, server-side configuration |

An unset or malformed password hash must disable login with a configuration error.
It must never enable an empty password. Generate the encoded hash with Django's configured
password hasher. Supply it through the ignored local `.env` or Railway variables.
The bot does not need the demo hash. The frontend receives no database credentials,
password hash, bot token or Django secret through Vite build variables.

## Session and CSRF contract

Use database-backed Django sessions. The demo login checks the supplied password against
`CRM_DEMO_PASSWORD_HASH` with Django's password checker. Log in one internal non-staff,
non-superuser Django principal with an unusable normal password. That principal exists
for session integration, not for public signup or individual employee accounts.
CRM permissions require this principal and a session access marker; an Admin login alone
does not grant demo access. Keep normal technical Admin authentication separate.

On a successful login, rotate the session key and CSRF token. Store `expires_at` as
login time plus 48 hours in UTC and set the session expiry to that absolute datetime.
Do not refresh it on requests. Use persistent cookies, with `HttpOnly`, `Secure` and
`SameSite=Lax` in production. Cookie Domain remains unset. Cookie Path is `/`.

Protect login and logout explicitly with Django CSRF even for anonymous requests.
DRF SessionAuthentication alone does not protect an anonymous login endpoint.
The session GET response supplies a masked token from Django `get_token()` because the
CSRF cookie is HttpOnly. Send it as `X-CSRFToken` with every POST. Send cookies with
same-origin requests. Obtain the rotated token from the successful login response.
Never return session keys in JSON. All session and CRM responses use `no-store`.

Logout flushes the current server session. Other browser sessions remain valid.
Broadcast a successful logout between same-origin tabs using BroadcastChannel; use the
storage event fallback when unavailable. The event contains no form data, token or cookie.
Close access behind a re-authentication dialog while retaining form values, submission
UUID and the frozen request snapshot in memory. On focus and on authentication failures,
recheck the session so missed notifications do not leave stale access visible.
After login in another tab, refresh the session and CSRF token before resuming a mutation.
Never automatically submit retained form data on login.

The API returns HTTP 401 for missing or expired CRM access. Adapt DRF's session
authentication/exception mapping to this contract; its default anonymous response can be
403. Reserve HTTP 403 for CSRF or permission rejection. Django's CSRF failure handler
must return the same JSON error envelope as the API. Authentication remains server-side.

## Public API

All routes below are planned and have a trailing slash. Bodies and responses are JSON.
Only session discovery, login and the existing minimal `/api/health/` route are public.
Logout is CSRF-protected and can also clear an already expired session. All lead and tag
routes require CRM access. Return errors as JSON, not redirects to Admin or HTML pages.

### Session routes

`GET /api/auth/session/` returns HTTP 200 for either state:

```json
{
  "authenticated": false,
  "expires_at": null,
  "csrf_token": "<masked-token>"
}
```

`POST /api/auth/login/` requires exactly one input field:

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

Tag identifiers are positive integers. A tag has `id`, `name` and `is_system`.
`GET /api/tags/` returns HTTP 200:

```json
{
  "results": [
    {"id": 1, "name": "Сайт", "is_system": true},
    {"id": 2, "name": "Реклама", "is_system": true},
    {"id": 3, "name": "Автоматизация", "is_system": true},
    {"id": 4, "name": "Другое", "is_system": true}
  ]
}
```

IDs in examples are illustrative. Seed and address system tags by stable codes `website`,
`advertising`, `automation` and `other`, not by an assumed primary key or translated name.
The direction-to-code mapping is fixed. Direction buttons carry codes; the lead receives
the corresponding persisted tag ID.

Lead IDs are UUID strings. `source` is `manual` or `telegram_bot` for P0.
`status` is `new` on creation; reserve `in_progress` and `closed` for P1.
The UI maps these codes to PRD labels. `created_at` is an immutable UTC ISO 8601 datetime.
Use `Intl.DateTimeFormat` with the device timezone and an explicit timezone label for
display. Do not turn the displayed local date back into the stored creation timestamp.

`GET /api/leads/{id}/` returns HTTP 200 with the lead representation:

```json
{
  "id": "7312e490-ae04-4f19-9f18-41d7d125db38",
  "name": "Тестовый клиент",
  "contacts": [{"type": "email", "value": "client@example.com"}],
  "request": "Нужен сайт агентства",
  "source": "manual",
  "status": "new",
  "created_at": "2026-10-01T12:00:00Z",
  "tags": [{"id": 1, "name": "Сайт", "is_system": true}]
}
```

An unknown lead returns 404 `not_found`. `GET /api/leads/` supports:

| Parameter | Contract |
| --- | --- |
| `tag_id` | Optional positive integer; one existing tag. Unknown tag returns 400 |
| `limit` | Integer 1–100; default 50 |
| `offset` | Integer >= 0; default 0 |

Sort by `created_at DESC, id DESC` to break ties consistently. Invalid query parameters
return 400 `validation_error`. Do not add P1 search or status filtering to the P0 contract.
The list returns the same lead objects:

```json
{
  "count": 0,
  "next": null,
  "previous": null,
  "results": []
}
```

`count` is the total matching the filter before pagination. `next` and `previous` are
relative `/api/leads/` URLs with the same filter and pagination parameters, or null.
Do not cache authenticated query results at the proxy.

Refresh the list every 5 seconds while the tab is visible and immediately on focus.
Do not overlap requests or apply an older response to a newer filter. Keep form state
independent from fetched list state. In P0, leads are append-only: the increase in matching
`count` gives the unseen-new count for an unchanged filter. Reset its baseline on filter
change and when the user accepts new items. Keep rendered row IDs and the scroll anchor
while below the top; do not blindly replace a live offset page after an insertion.
Refresh the needed pages on navigation. Stage 7 mutations must extend this reconciliation
before using count differences where deletion or changing tags can affect membership.

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
| 404 | `not_found` | Explain that the lead is unavailable |
| 409 | `submission_conflict` | Keep snapshot; explain conflict, never silently use a new UUID |
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

Deleting leads in P1 must preserve submission identity: a replay cannot resurrect a
deleted lead. Extend this contract with a deleted-result response before implementing
that feature. P0 does not expose deletion.

## Bot dialogue operations

The bot handles intake in private chats. Use one persistent active draft per Telegram
user. Persist `submission_id`, values, current step, revision, review/edit return state,
question message ID/date and submission state. Keep the last completed receipt reference
in a persistent BotUser row for `/start`. Lock this row for every draft mutation,
confirmation and consistent dialogue read. An active draft has submission state
`collecting`; a successful submission removes it and retains the receipt. A new draft
uses a new UUID. Valid field updates increment the revision.

Internal operations receive the Telegram user/chat identity and event identity from the
transport layer. They return structured outcomes; handlers map them to Telegram messages.

| Operation | Input | Result |
| --- | --- | --- |
| `get_dialogue` | User identity | Active draft, last completed receipt or empty state |
| `start_draft` | User identity, explicit restart flag | New UUID; restart removes the unsubmitted previous draft |
| `bind_question` | User identity, draft UUID/revision, successful outgoing Message ID/date | Persist the current question binding |
| `set_field` | User identity, draft UUID/revision, field, value, incoming event binding | Validated field and next step, or error with no transition |
| `begin_edit` | User identity, draft UUID/revision, selected field, optional contact index | Requested field; retain other values and return to review after correction |
| `remove_contact` | User identity, draft UUID/revision, contact index | Remove one contact; require replacement if none remain |
| `cancel_draft` | User identity, draft UUID/revision | Delete the active unsubmitted draft, or stale/submitted outcome |
| `confirm_draft` | User identity, draft UUID/revision | Shared transactional creation result or an earlier success receipt |

The normal steps are name → contacts → direction → request → review. After each accepted
contact, enter `contact_choice` and offer add/continue. Review can correct or remove an
individual contact; confirmation requires at least one. Drafts have no expiry. Input
errors remain
on the current step. `set_field` uses `contacts` to accept one contact and
`add_contact` / `continue_contacts` for contact-choice actions. Message input requires
its bound question; callbacks are checked by owner, UUID and revision. Transport must
never use the trusted internal `event=None` shortcut for incoming messages. Review
displays all values; confirmation uses that displayed revision.
After a correction, old review buttons are invalid. Persist mutations before acknowledging
them. Send the next question only after the field transaction succeeds. Bind it only after
Telegram confirms its delivery. If delivery/binding fails, keep the draft resumable and
resend the current question; do not consume subsequent input without a valid binding.

Use ForceReply for text questions. Accept an explicit reply only when its
`reply_to_message.message_id` matches the persisted current question in the same chat.
For unbound text or a contact-button response, require a message date strictly later than
the bound question date and the current expected input type. Earlier dates, stale explicit
replies and ambiguous same-second unbound messages do not advance the draft; show the
current question and request another answer. Do not assume message IDs are monotonic.

For the contact step, send the phone-sharing keyboard and then a ForceReply text prompt.
These are separate messages: Telegram allows only one reply markup per message. Bind the
text prompt as the question. Accept a contact-button response only from the expected user
(`contact.user_id` matches the sender); manual text accepts all PRD contact formats.
Remove the contact keyboard after leaving the step.

Callbacks carry a compact action, UUID and revision, within Telegram's 64-byte limit.
Verify the sender, current draft and revision for every mutation. A submitted receipt can
answer an old confirmation without mutating a new draft. A cancelled/unknown/stale button
returns an explanation and the current step. Do not implement callbacks using only field
names or direction labels without draft identity.

Keep update handling sequential for the single polling process. Store processed update
identity and the next polling offset in PostgreSQL. Advance the acknowledged offset only
after durable processing; an unprocessed event must remain retryable after a crash.
Use an explicit polling loop if the framework's default acknowledgement order cannot
provide this rule. On replay, do not reapply a field to a later step. Telegram sends occur
outside database transactions; a duplicate prompt or confirmation is preferable to lost
draft data or a duplicate lead. No delivery success may be claimed before it is known.

`/start` takes precedence over ordinary input. With an active draft, offer resume/restart.
With no active draft and a completed receipt, acknowledge the last submission and offer
a new one. With neither, show the initial invitation. A failed success message never
reopens a completed draft. Check the receipt before retrying uncertain confirmation.
Edits to past Telegram messages do not automatically edit stored draft fields; users use
the bot's correction actions.

## Implementation verification

These are required future checks, not test results from stage 1.

| Stage | Scenarios and required result |
| --- | --- |
| 2 | Restart preserves leads, tags, receipts and active drafts. Shared validation rejects PRD invalid input through manual creation and bot draft operations; API/transport adapters are checked at stages 4–5. Concurrent same-UUID creation commits one lead. A rollback permits retry; a lost response returns the original lead. A new UUID with equal data creates a separate lead. |
| 3 | Anonymous direct requests cannot read/write leads. Login and logout reject missing/wrong CSRF, including anonymous login. No secret appears in JSON/builds. Expiry is exactly 48 hours and does not slide. Reload/browser close does not shorten it. Logout invalidates only the current browser session and preserves another tab's form behind re-authentication. |
| 4 | Create with tags and without tags. Validation rejection preserves editable values. Uncertain result freezes fields and closure; retry opens one saved card. Re-authentication keeps UUID/snapshot and requires explicit retry. Timezone changes affect display only, including dates across midnight. |
| 5 | Cancel/restart creates no lead and deletes old draft values. Old callbacks cannot mutate a new draft or edited review. Rapid/unbound same-second messages do not skip fields. Phone button and manual contact both work. Prompt delivery/binding failures remain resumable. Crash before/after update acknowledgement does not lose accepted input. Lost success delivery and `/start` acknowledge one saved lead. |
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
- [Railway regions](https://docs.railway.com/deployments/regions).
- [Supabase database connections](https://supabase.com/docs/guides/database/connecting-to-postgres)
  and [SSL verification](https://supabase.com/docs/guides/database/psql).
- [Telegram Bot API](https://core.telegram.org/bots/api): ForceReply, KeyboardButton,
  Message, callback data and getUpdates acknowledgement.
