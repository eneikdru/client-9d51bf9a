# Время · booking service

A Russian-language booking application with persistent accounts, client history, master onboarding, editable calendars and a public booking link. Python 3.12+, FastAPI, SQLite, buildless HTML/CSS/JS. The app is live by default: synthetic masters and demo login are disabled unless ALLOW_DEMO=true is explicitly set for testing.

## Use the application

1. Create an account with a unique login (3–32 Latin letters/digits/._-), a display name, and a password of 12–128 characters. Choose client or master.
2. Save the private recovery code displayed once after registration. Recovery is code-based; no email verification or email-delivery service is implied.
3. Clients choose a published master, a service, date and available time, then confirm. Visits stay in **Мои записи** after logout and login. Future bookings can be cancelled.
4. Masters complete three onboarding steps: public name/timezone, first service and price, and normal working week. Publishing creates an actual profile; fictional examples are not automatically published.
5. **Кабинет мастера** provides a shareable booking link, publication toggle, daily agenda, Today/Tomorrow shortcuts, working intervals, days off, copying to the next six dates, and reset to the weekly template. Services and the normal week remain editable.
6. **Как это работает** reopens short contextual instructions. Dismissing the first-use guide is saved to the account.

Pausing publication hides the profile from the public catalog and stops new customer bookings. Existing appointments and customer cancellation rights remain intact. Authentication, bookings, profile settings and recovery never depend on browser-local demo data.

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:BOOKING_DATA_DIR = 'C:\booking-data'
$env:COOKIE_SECURE = 'false' # local HTTP only
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Without BOOKING_DATA_DIR, state is stored in `~/.local/share/efremov-booking/client-9d51bf9a`, outside the replaceable checkout. A relative DATABASE_PATH is resolved there; an absolute DATABASE_PATH is honored. Account data survives application releases as long as that persistent directory/volume remains available. SESSION_SECRET can be supplied by the operator (32+ characters); otherwise an atomic, permission-restricted `session.key` is generated once in the state directory. Keep it across restarts. Changing an explicitly configured SESSION_SECRET invalidates browser cookies but does not delete accounts or appointments.

`GET /api/status` exposes an opaque installation_id persisted in SQLite so release checks can confirm they reach the same data store. It never exposes filesystem paths, secrets or credentials.

## Deploy

The existing EFREMOV gateway commits and deploys the repository. Source changes go through `/api/v1/bridge/commit`; runtime SQLite files, private signing keys and account credentials must never be committed or sent with source payloads. The UI and Swagger use mount-relative URLs and support the preview prefix.

For an independent host, `docker compose up --build -d` mounts the named `booking-data` volume at `/data` and exposes port 8000 only to localhost. Put an HTTPS reverse proxy in front. Production cookies are Secure and HttpOnly; COOKIE_SECURE=false is solely for local HTTP development. Use a stable persistent volume and make SQLite backups. A copied source tree alone does not contain user data.

Set ROOT_PATH when a proxy strips the external prefix, for example `/api/preview/9d51bf9a`. PUBLIC_BASE_URL is the exact external HTTPS base URL including its prefix for optional OAuth callbacks. Trust forwarded headers only from your actual reverse proxy. The current preview gateway supports GET/POST; management updates expose POST aliases alongside their PUT routes. Both methods run the same validation, CSRF, ownership and optimistic revision checks.

The UI deliberately versions its CSS/JS URLs and sends revalidation headers. HTML and API responses are marked no-store. Manrope is loaded from Google Fonts with a system-font fallback.

### Backups

Back up through SQLite's backup API, not by copying a live database file mid-write:

```python
import sqlite3
with sqlite3.connect('/data/booking.sqlite3') as source:
    with sqlite3.connect('/backup/booking-backup.sqlite3') as destination:
        source.backup(destination)
```

Protect backups as account data. Restore with the service stopped, preserve file ownership, and retain the matching signing key or explicitly force users to log in again. The repository does not provision off-site backup storage or a custom domain.

## Identity and access

Passwords use salted scrypt (N=32768, r=8, p=3), one of the documented [OWASP configurations](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html). Recovery codes and opaque agent/session tokens are stored only as SHA-256 digests. Failed sign-in/recovery and signup requests are rate limited in SQLite. Local sessions are server-side, expire after seven days, and are revoked on logout; recovery rotates the code and invalidates all the user's sessions and agent tokens. Validation responses never echo submitted passwords or recovery codes. Credentials are not logged by application code.

Browser mutations, including registration/login, require the CSRF token from `/api/config` or `/api/me`. User identity is never accepted from a caller-supplied booking field. Master writes require the profile owner and a browser session; delegated agent tokens cannot change calendars or account settings. The role picker is not an administrator grant: masters can only create/manage their own profiles.

Google/Facebook login are optional. Configure GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET and FACEBOOK_CLIENT_ID/FACEBOOK_CLIENT_SECRET plus FACEBOOK_GRAPH_VERSION to a supported version from the Meta dashboard. Register exact HTTPS callback URLs. Missing providers are hidden; local accounts remain usable. Authlib verifies OAuth state and Google's ID token; Facebook identity is obtained server-side through Graph `/me` with appsecret_proof. Live provider setup and review are separate from local account sign-in. Accounts are not silently merged by matching names or email.

## Calendar and booking invariants

- UTC epoch seconds in storage; IANA timezone per master. Prices are integer GEL minor units.
- A working week has keys 0 (Monday) through 6 (Sunday). A day override supersedes it; an empty override is a day off. Reset removes the override.
- Multiple intervals express lunch and other breaks. Intervals cannot overlap or cross midnight; split overnight work across dates. Nonexistent DST wall times are skipped; ambiguous times use their first occurrence.
- Availability fits the full service plus its trailing buffer in a working interval, respects minimum notice and booking horizon, and excludes confirmed appointments across all the master's services.
- Slot snapshots are immutable. Service price/duration changes apply only to new appointments. Start times materialized from obsolete master/service revisions are rejected when booked.
- Booking creation, cancellation and schedule changes use BEGIN IMMEDIATE. Competing overlapping bookings yield one success and a 409 conflict. Idempotency-Key repeats return the original booking, including after cancellation or settings edits.
- Schedule edits cannot exclude an existing future appointment or its buffer. Batch date edits (up to 14 dates) are atomic and roll back together on a conflict. Revision checks prevent lost edits from another browser tab.
- Cancellation is terminal and repeatable; a started/finished visit cannot be cancelled. The master agenda shows the customer's display name only to that master.

## API and agent boundary

`/docs` and `/openapi.json` describe the API. `/api/catalog` and published availability can be read before sign-in; booking/history require a permanent identity. `/api/onboarding/master` atomically creates the first master, service and publication and is idempotent per profile.

POST `/api/agent-token` issues a one-hour delegated bearer token from an authenticated browser session. It can read/create/cancel that user's bookings; it cannot issue more tokens or edit master/account settings. POST `/api/bookings` requires `{"slot_id":"..."}` and Idempotency-Key. All clients use the same atomic booking engine.

A ready-to-connect Claude/Gemini MCP server, OAuth authorization-server account linking, Meta AI connector registration, payments, messaging and email/SMS notifications are not implemented by this release. Local account sign-in must not be advertised as an activated external-agent connector.

## Verification

```text
python -m pytest -q
node --check calendar.js
python -m compileall -q app.py accounts.py scheduling.py storage.py
```

Tests run against isolated temporary databases. They cover real registration/login/recovery, password validation and non-disclosure, throttling, session/token revocation, two independent client/master identities, onboarding atomicity/idempotency, publication, persistent identity, concurrency, rollback, ownership, CSRF, calendar revisions, buffers, breaks, DST, mounted routes, asset caching and storage/key initialization across releases. Optional OAuth callbacks use mocks; tests do not establish real provider credentials.

## Release persistence check

After deployment, compare `/api/status` installation_id with its pre-deployment value, then log in with a previously registered account and verify its master ID and booking history. This tests the actual host storage boundary; a successful source commit alone is not evidence of durable account data. QA profiles are unpublished after integration checks.
