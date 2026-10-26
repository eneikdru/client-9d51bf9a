# client-9d51bf9a
Dedicated site repository for dmitrii.eneik@gmail.com under Dmitry Efremov architecture

## Booking MVP

Python 3.12+, FastAPI, SQLite. This is a locally tested foundation, not an activated Meta AI connector.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:SESSION_SECRET = python -c "import secrets; print(secrets.token_urlsafe(48))"
$env:COOKIE_SECURE = 'false' # local HTTP only; production default is true
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET and FACEBOOK_CLIENT_ID / FACEBOOK_CLIENT_SECRET in the process environment. Set FACEBOOK_GRAPH_VERSION to a supported version from your Meta application dashboard. Register exact HTTPS callbacks `/auth/google/callback` and `/auth/facebook/callback`. Production requires HTTPS, a persistent SESSION_SECRET and DATABASE_PATH on persistent storage. Configure trusted reverse-proxy forwarding explicitly; never trust arbitrary forwarded hosts. OAuth uses server-side code exchange, state verification, and Google ID-token validation through Authlib. Facebook uses Graph `/me` and appsecret_proof. Real provider login requires app credentials and review; mocks do not verify provider configuration.

Create availability through the trusted operator CLI (no public master administration):

```text
python manage.py slot-1 master-1 Haircut 1800000000 1800003600 5000
```

Times are UTC epoch seconds. End includes the master's buffer. Prices are integer GEL minor units. Master IDs must be globally unique. A slot belongs to one master; bookings belong to the signed-in provider subject, never to a caller-supplied user ID. Separate provider accounts are intentionally not merged by email. Intervals are half-open; SQLite BEGIN IMMEDIATE serializes writers, including overlapping distinct slots. Cancellation is terminal and repeatable; a confirmed appointment cannot be cancelled once it has started. Reusing a creation idempotency key returns the original booking, including cancelled state.

Open `/` for the Russian booking UI, `/docs` or `/openapi.json` for API definitions. Browser mutations require X-CSRF-Token from `/api/me`. POST `/api/agent-token` issues a one-hour opaque bearer token for a trusted agent; only its hash is stored. Token issuance requires a browser session (bearer tokens cannot mint more tokens); logout revokes that user's agent tokens. Send Authorization: Bearer TOKEN to list/create/cancel your bookings. POST `/api/bookings` accepts `{"slot_id":"slot-1"}` and requires Idempotency-Key. No payment or calendar integration is included.

## Meta AI integration boundary

https://dev.meta.ai/products/connectors documents API onboarding and OAuth account linking under early access. This repository supplies booking actions, but does **not** yet implement an OAuth authorization server for Meta account linking or register a connector. Do not pass a shared service credential or a user ID from model output as booking authorization. Once Meta grants access, implement its approved account-linking contract and register the OpenAPI actions; booking must use the linked user's delegated token. The manual short-lived token is for API testing, not a substitute for Meta account linking.

## Verification and architecture

`python -m pytest -q` covers unauthorized/expired tokens, missing resources, past slots, invalid inputs, CSRF, OAuth identity handling, cross-user access, terminal cancellation, idempotency conflicts, adjacent intervals, other masters, and simultaneous booking attempts. Auth flows use mocks; live OAuth and Meta end-to-end tests remain dependent on credentials/access. The file scope is limited to the booking MVP. No external calendar, payments, messaging, or unrelated schema changes. Database invariants and executable tests enforce the handshake's scope, falsification, interval and ownership rules.


## Demo and proxy deployment (repair)

The home screen reports configured providers and offers **Попробовать демозапись**. Missing OAuth configuration no longer sends users to a raw JSON error. Demo mode creates a unique browser session after a CSRF-protected request. It uses only synthetic `demo:` slots, visibly marked as fictional; it cannot book live slots, and real accounts cannot book demo slots. Existing live sessions cannot silently switch into demo mode. Nine rolling slots for tomorrow are seeded under reserved IDs without overwriting real availability. `manage.py` refuses that reserved namespace. Demo sessions share synthetic availability to exercise actual collision handling. This is not a replacement for real OAuth or Meta account linking.

For a mounted ASGI app the mount supplies root_path automatically. For a stripping reverse proxy set ROOT_PATH to the external prefix (for example `/api/preview/9d51bf9a`) or run uvicorn with `--root-path`. Set PUBLIC_BASE_URL to the exact external HTTPS base URL including any prefix for OAuth callbacks. It is configuration controlled by the operator, not a value accepted from arbitrary request headers. Callback redirects and disabled-provider redirects are relative to their current route. The UI uses relative navigation and fetch URLs; Swagger loads `./openapi.json` and adjusts same-origin Try-it-out calls to the visible mount even if a proxy does not supply root_path.

SQLite connections close after both successful and failed transactions. Creation and cancellation acquire BEGIN IMMEDIATE; output models constrain slot and booking schemas. Storage errors return a generic 503 with Retry-After instead of leaking internal paths. Tests include an end-to-end demo flow, demo/live isolation, mounted callbacks/docs, repeated seeding, simultaneous cancellation, same-key races, overlapping-slot races, provider failures and token revocation.


## Editable calendar and master workspace

The UI now uses `/api/masters` and `/api/calendar/availability`, not the legacy fixed-slot list. In demo mode each browser identity receives a private editable set of three fictional masters and six services. The initial schedule is Monday–Friday 10:00–13:00 / 14:00–19:00, Saturday 10:00–15:00, Sunday closed, Asia/Tbilisi timezone. Services have distinct duration, buffer and price. Defaults are inserted only once per workspace; edits persist across reloads and application restarts as long as the database and session remain available. Logging out creates a new demo identity on next entry; live OAuth accounts provide persistent identity.

**Кабинет мастера** has a daily editor, Today/Tomorrow shortcuts, multiple work intervals, a day-off action, optional copying to the next six dates, and reset to the weekly template. The weekly template, display name, IANA timezone, minimum notice, booking horizon and start-time step are editable. Services can be created, repriced, resized or deactivated. Intervals cannot cross midnight or overlap; split overnight work between two dates. DST nonexistent wall times are excluded; ambiguous times use their first occurrence.

A day override is distinct from the weekly template: an empty override is a day off, while reset removes the override. Working windows must contain the entire service plus its trailing buffer. Availability excludes all confirmed appointments of the master, across services. Booking creation revalidates current master/service revisions and scheduling rules inside BEGIN IMMEDIATE. Schedule changes use the same transaction lock and reject any change that excludes an existing future appointment, including its buffer. Price/duration edits affect new bookings; existing slot snapshots are immutable. Replayed idempotent booking requests still return their original booking after settings change.

Management writes require the master's browser identity plus CSRF and optimistic revision checks. Bearer booking tokens cannot edit master settings. Demo workspaces cannot read or book other demos or live masters; existing legacy booking endpoints remain compatible. Batch date edits are atomic, limited to fourteen dates, and roll back if any included booking would be excluded. Anonymous users cannot mutate schedules. The public demo remains clearly marked and is not real provider authentication or an external agent connector.

`python -m pytest -q` now includes calendar generation, durations, buffers and breaks, daily reset, weekly edits, revision conflicts, unchanged booking snapshots, failed batch rollback, cross-workspace access, agent restrictions, invalid schedule data, DST and overlapping-start concurrency. Run `node --check calendar.js` for browser script syntax.

The preview gateway only forwards GET/POST. Calendar management update routes therefore expose equivalent POST aliases in addition to PUT; the browser uses POST. The same schemas, CSRF, ownership and revision checks apply to both methods.
