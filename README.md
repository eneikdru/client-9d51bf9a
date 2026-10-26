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

Times are UTC epoch seconds. End includes the master's buffer. Prices are integer GEL minor units. Master IDs must be globally unique. A slot belongs to one master; bookings belong to the signed-in provider subject, never to a caller-supplied user ID. Separate provider accounts are intentionally not merged by email. Intervals are half-open; SQLite BEGIN IMMEDIATE serializes writers, including overlapping distinct slots. Cancellation is terminal and repeatable. Reusing a creation idempotency key returns the original booking, including cancelled state.

Open `/` for the Russian booking UI, `/docs` or `/openapi.json` for API definitions. Browser mutations require X-CSRF-Token from `/api/me`. POST `/api/agent-token` issues a one-hour opaque bearer token for a trusted agent; only its hash is stored. Send Authorization: Bearer TOKEN to list/create/cancel your bookings. POST `/api/bookings` accepts `{"slot_id":"slot-1"}` and requires Idempotency-Key. No payment or calendar integration is included.

## Meta AI integration boundary

https://dev.meta.ai/products/connectors documents API onboarding and OAuth account linking under early access. This repository supplies booking actions, but does **not** yet implement an OAuth authorization server for Meta account linking or register a connector. Do not pass a shared service credential or a user ID from model output as booking authorization. Once Meta grants access, implement its approved account-linking contract and register the OpenAPI actions; booking must use the linked user's delegated token. The manual short-lived token is for API testing, not a substitute for Meta account linking.

## Verification and architecture

`python -m pytest -q` covers unauthorized/expired tokens, missing resources, past slots, invalid inputs, CSRF, OAuth identity handling, cross-user access, terminal cancellation, idempotency conflicts, adjacent intervals, other masters, and simultaneous booking attempts. Auth flows use mocks; live OAuth and Meta end-to-end tests remain dependent on credentials/access. The file scope is limited to the booking MVP. No external calendar, payments, messaging, or unrelated schema changes. Database invariants and executable tests enforce the handshake's scope, falsification, interval and ownership rules.
