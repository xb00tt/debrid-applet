# Auth errors during outages — Design

Date: 2026-10-13
Base: debrid-applet 1.2.0. Target version: 1.2.1. Bounded change: the existing invalid-key flow, no new subsystem.

## Problem

One `Invalid API key` answer opens Settings (`ServiceTab.say` → `App.open_settings_once`). On 2026-10-13 at 17:17:51 Torbox
returned it once, in the middle of an outage (timeouts and "There was an error processing your request" for twenty
minutes before), with a valid key: the dialog popped up for nothing. The activity log only shows the mapped message,
so nothing says which HTTP status or service code produced it.

Two mappings in `debrid_api.py` can turn a non-auth failure into `INVALID_KEY`:

- `_req()`: any **403 whose body is not JSON** → `INVALID_KEY` (a guess from the first release; a CDN or proxy error
  page during an outage is exactly a 403 with an HTML body).
- `_is_auth_error()`: Torbox `NO_AUTH`, `BAD_TOKEN`, `AUTH_ERROR` and any `AUTH_*` code → `INVALID_KEY`. Torbox
  publishes no definition of these codes (checked 2026-10-13: api-docs.torbox.app lists none, the SDKs name none), so
  whether `AUTH_ERROR` can mean "our auth service is down" is unknown.

## Goal

Settings opens only for a key the service rejects twice; every invalid-key message carries the evidence; a 403 without
a JSON body is a plain HTTP error.

## Design

### 1. Evidence in the message (`debrid_api.py`)

`INVALID_KEY` stays `"Invalid API key"` as the prefix; the raised message appends what was seen:

- 401 → `Invalid API key (HTTP 401)`
- 403 with a JSON auth body → `Invalid API key (HTTP 403 BAD_TOKEN)` (the code, or the message when no code)
- 403 with a non-JSON body → **no longer an auth error**: `HTTP 403: <first 200 chars>` via `_error_message`
- Torbox / AllDebrid `success: false` auth body → `Invalid API key (BAD_TOKEN)` / `Invalid API key (AUTH_BAD_APIKEY)`

`debrid_api.is_invalid_key(message)` → `message.startswith(INVALID_KEY)`; every consumer uses it instead of `==`.
Consumers: `ServiceTab.say`, the Settings "Test keys" result (shows the message with the evidence, which is now more
useful), tests.

### 2. Confirmation before Settings (`debrid_applet.py`)

`ServiceTab.say(msg, error=True)` with an invalid-key message no longer opens Settings. It calls
`self._confirm_auth(msg)`:

- At most one confirmation per tab per `AUTH_CONFIRM_EVERY = 300` seconds (`self._auth_checked_at`, monotonic);
  within the window the error is only shown and logged.
- Off-thread, `self.client.account()` once. On the main loop:
  - the check raises an invalid-key message too → log `Invalid API key confirmed by a second request: <first> / <second>`
    and `self.app.open_settings_once()` (unchanged: once per key set);
  - the check succeeds or fails otherwise → log `Transient auth error ignored: <first>; check said: <second or OK>`,
    nothing opens.
- The status line keeps showing the red message either way; the tooltip keeps the last errors.

Pure helpers so the policy is testable without GTK: `should_confirm_auth(last_at, now, every=AUTH_CONFIRM_EVERY)`
and `auth_confirmed(check_error)` (`check_error is not None and is_invalid_key(check_error)`).

Not changed: `_is_auth_error` keeps the Torbox and AllDebrid code lists (their meaning is undocumented; the
confirmation is the guard), "Test keys" in Settings (direct `account()` calls, reported in the dialog), the merged
All tab (it never calls a service).

### 3. Diagnosis next time

The error line in the activity log now reads e.g. `ERROR Invalid API key (HTTP 403)` or `ERROR HTTP 403: <html…`
followed by the confirmation line, so the next incident names its status and code. `TODO.md` keeps a note to
revisit `AUTH_ERROR` once a real incident shows it.

## Tests (`test_debrid_api.py`)

- `test_invalid_key`: the 401 and 403-JSON cases assert `is_invalid_key(str(e))` and the evidence suffix; the
  403-non-JSON case moves out and asserts a plain `HTTP 403:` message.
- `test_tb_bad_token_is_invalid_key` and the AllDebrid auth test: `is_invalid_key` plus the code in the message.
- `test_403_with_service_message_is_not_invalid_key`: unchanged.
- New `test_auth_confirmation_policy`: `should_confirm_auth` (first time, inside the window, after it) and
  `auth_confirmed` (invalid-key message, other error, `None`).
- `test_local_api` / anything comparing `== "Invalid API key"` is switched to `is_invalid_key`.

Manual, on this box: temporarily put a wrong Torbox key in a scratch config (`DEBRID_APPLET_CONFIG_DIR`, keys never
printed) and run the applet from the repo: first poll shows the red message with `(HTTP 403 BAD_TOKEN)` or similar,
the log shows the confirmation line, Settings opens once. Then with the real config and a simulated transient error
(not reproducible on demand; covered by the unit tests of the policy).

## Risks

- A genuinely bad key costs one extra `account()` request before Settings opens; a few seconds.
- During a long outage with a service answering `AUTH_*` consistently, the confirmation also fails and Settings opens
  once per key set, as today. The 5-minute throttle keeps the extra requests to one per tab per window.
- If a service's auth codes turn out to carry a transient meaning, the log now shows it and the code list can shrink.

## Size

About 40 lines across `debrid_api.py` (two mappings, one helper) and `debrid_applet.py` (confirmation, two helpers),
test edits, one TODO note. No packaging or docs changes beyond the MANUAL troubleshooting line "Tabs say Not
configured" gaining "the log names the status and code".
