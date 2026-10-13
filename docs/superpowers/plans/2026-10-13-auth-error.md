# Auth errors during outages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Settings opens only for a key a service rejects twice; invalid-key messages name the HTTP status and code; a 403 without a JSON body is a plain HTTP error.

**Architecture:** `debrid_api.py` keeps raising `ApiError` with the `Invalid API key` prefix but appends evidence, drops the 403-non-JSON guess, and exports `is_invalid_key()`. `ServiceTab.say()` hands invalid-key messages to `_confirm_auth()`, which runs one `account()` call off-thread (throttled to one per tab per five minutes) and opens Settings only when that call is rejected too. The policy lives in two pure helpers with tests.

**Tech Stack:** Python 3.12, existing offline suite (`python3 test_debrid_api.py`, FakeSession/FakeResp), GTK only in the manual step.

**Spec:** `docs/superpowers/specs/2026-10-13-auth-error-design.md`

**Execution:** Native (chosen by the user). Final whole-branch review on `opus`. Lookups, if any, on Haiku.

## Global Constraints

- `INVALID_KEY` stays `"Invalid API key"`; raised messages are `INVALID_KEY` plus ` (evidence)`; every consumer matches with `is_invalid_key()` (prefix), never `==`.
- Evidence formats, exact: `Invalid API key (HTTP 401)`, `Invalid API key (HTTP 403 BAD_TOKEN)`, `Invalid API key (BAD_TOKEN)` for a 200 body, `Invalid API key (AUTH_BAD_APIKEY)` for AllDebrid; when a 401/403 body has no code: `Invalid API key (HTTP 401)`.
- A 403 whose body is not JSON raises `_error_message(r)` (`HTTP 403: <first 200 chars>`), not an auth error.
- `AUTH_CONFIRM_EVERY = 300` seconds. Log lines, exact prefixes: `Invalid API key confirmed by a second request: ` and `Transient auth error ignored: `.
- `TORBOX_AUTH_ERRORS` and the `AUTH_*` prefix rule are unchanged.
- `VERSION` becomes `1.2.1` only in Task 3.
- The suite sets `DEBRID_APPLET_CONFIG_DIR` / `DEBRID_APPLET_DATA_DIR` before importing `debrid_applet`; the manual step uses a scratch config dir with a made-up Torbox key and never prints or copies the real keys.
- Line numbers refer to the 1.2.0 tree; locate by the quoted code.

## Review Focus

1. Real-Debrid 403 with a JSON permission message (`test_403_with_service_message_is_not_invalid_key`) must stay a plain message after the 403 branch is rewritten. Pinned by the existing test, re-run in Task 1.
2. `retryable()` must not match the new messages (an invalid key is not retried). Pinned in Task 1's test addition.
3. A confirmation finishing after Settings "Save" rebuilt the tabs: the callback runs on a tab no longer in `app.tabs`; it must only log and call `open_settings_once` through the app, never crash. Pinned by the guard in Task 2 (`if self not in self.app.tabs: return`) and a line in the manual step.
4. The first confirmation must not be throttled (`last_at is None`). Pinned in Task 2's test.
5. A tab without a client (service not configured) never confirms. Pinned by the guard in Task 2.

---

### Task 1: Evidence in the API layer, 403 without JSON is not an auth error

**Files:**
- Modify: `debrid_api.py:100-107` (`_req` 401/403), `:151-154` (`_is_auth_error`, add `_auth_code`), `:299-300` (Torbox `_call`), `:464-465` (AllDebrid `_call`); export `is_invalid_key`
- Test: `test_debrid_api.py` (`test_invalid_key` :287, `test_ad_add_link_and_errors` :455, `test_tb_bad_token_is_invalid_key` :688; append `test_invalid_key_evidence`)

**Interfaces:**
- Consumes: `INVALID_KEY`, `_json_or_none`, `_error_message`.
- Produces: `is_invalid_key(message) -> bool`; `_auth_code(body) -> str` (the code string or `""`); messages with evidence.

- [ ] **Step 1: Write the failing tests**

Edit `test_invalid_key` to:

```python
def test_invalid_key():
    from debrid_api import is_invalid_key
    cases = [
        (RealDebrid("t", FakeSession(FakeResp(401, {"error": "bad_token", "error_code": 8}))), "Invalid API key (HTTP 401 bad_token)"),
        (Torbox("t", FakeSession(FakeResp(403, {"success": False, "error": "BAD_TOKEN", "detail": "bad", "data": None}))), "Invalid API key (HTTP 403 BAD_TOKEN)"),
        (RealDebrid("t", FakeSession(FakeResp(401, None))), "Invalid API key (HTTP 401)"),
    ]
    for c, expected in cases:
        try:
            c.account()
            assert False, "expected ApiError"
        except ApiError as e:
            assert str(e) == expected and is_invalid_key(str(e))
    try:  # a 403 without a JSON body is a CDN or proxy page, not a verdict on the key
        Torbox("t", FakeSession(FakeResp(403, None))).account()
        assert False
    except ApiError as e:
        assert str(e).startswith("HTTP 403") and not is_invalid_key(str(e))
```

`FakeResp(403, None)`: check how `FakeResp` renders `None` (line 24-35); if `.json()` raises `ValueError` and `.text` is a string, nothing else is needed.

In `test_ad_add_link_and_errors`, replace `assert str(e) == "Invalid API key"` with `assert str(e) == f"Invalid API key ({code})"`.
In `test_tb_bad_token_is_invalid_key`, replace `assert str(e) == "Invalid API key"` with `assert str(e) == "Invalid API key (BAD_TOKEN)"`.

Append before `if __name__ == "__main__":`:

```python
def test_invalid_key_evidence():
    import debrid_api as api
    import debrid_applet as da
    assert api.is_invalid_key("Invalid API key") and api.is_invalid_key("Invalid API key (HTTP 403 BAD_TOKEN)")
    assert not api.is_invalid_key("HTTP 403: <html>") and not api.is_invalid_key("Network error: x")
    assert api._auth_code({"error": "BAD_TOKEN"}) == "BAD_TOKEN" and api._auth_code({"error": {"code": "AUTH_X"}}) == "AUTH_X"
    assert api._auth_code({"error": None}) == "" and api._auth_code({}) == ""
    assert not da.retryable("Invalid API key (HTTP 401)")  # a bad key is never retried


```

- [ ] **Step 2: Run the suite to verify it fails**

Run: `python3 test_debrid_api.py`
Expected: `ImportError: cannot import name 'is_invalid_key' from 'debrid_api'` in `test_invalid_key` (earlier tests `ok`).

- [ ] **Step 3: API changes**

In `debrid_api.py`, after `INVALID_KEY = "Invalid API key"` add:

```python


def is_invalid_key(message):
    """The service rejected the key: INVALID_KEY plus the evidence in brackets (status, code)."""
    return str(message).startswith(INVALID_KEY)
```

Replace `_is_auth_error`:

```python
def _auth_code(body):
    """The error code string of a service body: Torbox 'error': 'CODE', AllDebrid 'error': {'code': …}, Real-Debrid
    'error': 'bad_token'; '' when absent."""
    err = body.get("error") if isinstance(body, dict) else None
    code = err.get("code", "") if isinstance(err, dict) else err
    return str(code or "")


def _is_auth_error(body):
    code = _auth_code(body)
    return code in TORBOX_AUTH_ERRORS or code.startswith("AUTH_")
```

Replace the 401/403 block in `_req`:

```python
        if r.status_code in (401, 403):
            body = _json_or_none(r)
            code = _auth_code(body)
            if r.status_code == 401 or (body is not None and _is_auth_error(body)):
                raise ApiError(f"{INVALID_KEY} (HTTP {r.status_code}{' ' + code if code else ''})")
            # 403 with a service message (Real-Debrid permission errors) or without JSON (a CDN page): not a verdict on the key
            raise ApiError(_error_message(r))
```

Torbox `_call` and AllDebrid `_call`: `raise ApiError(INVALID_KEY)` → `raise ApiError(f"{INVALID_KEY} ({_auth_code(body)})")`.

- [ ] **Step 4: Run the suite**

Run: `python3 test_debrid_api.py`
Expected: `92 passed`. `test_403_with_service_message_is_not_invalid_key` still `ok`.

- [ ] **Step 5: Commit**

```bash
git add debrid_api.py test_debrid_api.py
git commit -m "Invalid-key errors carry their status and code; a 403 without JSON is a plain HTTP error"
```

---

### Task 2: Confirm before opening Settings

**Files:**
- Modify: `debrid_applet.py:47` (import), `:788` (after `retryable`), `:1167` (ServiceTab state), `:1502-1503` (`say`)
- Test: `test_debrid_api.py` (append `test_auth_confirmation_policy`)

**Interfaces:**
- Consumes: `is_invalid_key` from Task 1; `run_async`, `log_event`, `App.open_settings_once`.
- Produces: `AUTH_CONFIRM_EVERY = 300`; `should_confirm_auth(last_at, now, every=AUTH_CONFIRM_EVERY) -> bool`; `auth_confirmed(check_error) -> bool`; `ServiceTab._auth_checked_at` (`None` until the first check); `ServiceTab._confirm_auth(msg)`.

- [ ] **Step 1: Write the failing test**

```python
def test_auth_confirmation_policy():
    import debrid_applet as da
    assert da.should_confirm_auth(None, 1000.0)  # first error: always check
    assert not da.should_confirm_auth(1000.0, 1100.0)  # inside the window: show the error, no second request
    assert da.should_confirm_auth(1000.0, 1000.0 + da.AUTH_CONFIRM_EVERY)
    assert da.should_confirm_auth(1000.0, 1010.0, every=5)
    assert da.auth_confirmed("Invalid API key (HTTP 403 BAD_TOKEN)")
    assert not da.auth_confirmed("Network error: timeout") and not da.auth_confirmed(None)


```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 test_debrid_api.py`
Expected: `AttributeError: module 'debrid_applet' has no attribute 'should_confirm_auth'`.

- [ ] **Step 3: Helpers**

Line 47: add `is_invalid_key` to the `from debrid_api import (…)` list and drop `INVALID_KEY` from it if nothing else uses it (`grep -n INVALID_KEY debrid_applet.py` after the edit must show only the import, then remove it there too).

After `retryable()` add:

```python
AUTH_CONFIRM_EVERY = 300  # seconds between confirmation requests per service after an invalid-key answer


def should_confirm_auth(last_at, now, every=AUTH_CONFIRM_EVERY):
    """One account() call confirms an invalid-key answer before Settings opens; never more often than every."""
    return last_at is None or now - last_at >= every


def auth_confirmed(check_error):
    """The confirming account() call failed the same way: the key really is rejected."""
    return check_error is not None and is_invalid_key(check_error)
```

- [ ] **Step 4: The tab side**

After `self._updated_at = 0  # …` (line 1167) add:

```python
        self._auth_checked_at = None  # time.monotonic() of the last invalid-key confirmation request
```

In `say()`, replace:

```python
        if error and msg == INVALID_KEY:
            self.app.open_settings_once()
```

with:

```python
        if error and is_invalid_key(msg):
            self._confirm_auth(msg)
```

Add after `say()`:

```python
    def _confirm_auth(self, first):
        """A service said the key is invalid. Outages have said so with a valid key (Torbox, 2026-10-13), so ask once
        more with account(); Settings opens only when that answer is a rejection too."""
        now = time.monotonic()
        if self.client is None or not should_confirm_auth(self._auth_checked_at, now):
            return
        self._auth_checked_at = now

        def done(_account, err):
            if self not in self.app.tabs:  # Settings rebuilt the tabs meanwhile; nothing to show here
                return
            if auth_confirmed(err):
                log_event(self.name, f"Invalid API key confirmed by a second request: {first} / {err}", "error")
                self.app.open_settings_once()
            else:
                log_event(self.name, f"Transient auth error ignored: {first}; check said: {err or 'OK'}", "error")
        run_async(self.client.account, done)
```

- [ ] **Step 5: Run the suite**

Run: `python3 test_debrid_api.py` → `93 passed`. `python3 -m py_compile debrid_applet.py`.

- [ ] **Step 6: Manual check with a wrong key in a scratch config**

Quit the installed applet (same bus name; a repo launch would just forward to it). Then:

```bash
S=/home/borislav/github/debrid-applet/.superpowers/sdd/2026-10-13-auth-error/scratch; mkdir -p $S
python3 -c "import json; json.dump({'torbox_key': 'wrong-key-for-the-manual-test', 'config_version': 2}, open('$S/config.json', 'w'))"
DEBRID_APPLET_CONFIG_DIR=$S DEBRID_APPLET_DATA_DIR=$S python3 debrid_applet.py --toggle
```

Expected within a few seconds: the status line shows `Invalid API key (HTTP 403 BAD_TOKEN)` (or whatever Torbox answers; the log has it), `$S/activity.log` has `Invalid API key confirmed by a second request: …`, and the Settings dialog opens once. Press Escape, Ctrl+Q, relaunch the installed applet with `gtk-launch debrid-applet`, delete `$S`.

The transient case cannot be provoked on demand; the policy test covers it.

- [ ] **Step 7: Commit**

```bash
git add debrid_applet.py test_debrid_api.py
git commit -m "An invalid-key answer is confirmed with a second request before Settings opens"
```

---

### Task 3: Docs and release 1.2.1

**Files:**
- Modify: `docs/MANUAL.md:187`, `debrid_applet.py:52` (`VERSION`), `README.md:14`, `io.github.xb00tt.debrid-applet.metainfo.xml`, `packaging/io.github.xb00tt.debrid-applet.yml` (`tag`)

- [ ] **Step 1: MANUAL**

Replace line 187 with:

```
- **Tabs say "Not configured"**: open Settings → Accounts and check the keys; Test keys tells you which one fails.
  Settings opens by itself only after a service rejected the key twice in a row; the activity log names the HTTP
  status and code it answered with (`Invalid API key (HTTP 403 BAD_TOKEN)`), and a one-off rejection during an outage
  is logged as "Transient auth error ignored".
```

- [ ] **Step 2: Bump and watch the tests demand the rest**

`VERSION = "1.2.1"`, README `debrid-applet_1.2.1_all.deb`. Run `python3 test_debrid_api.py` → `AssertionError` in `test_metainfo` (first release is 1.2.0).

- [ ] **Step 3: Release entry, screenshot tag, manifest tag**

Metainfo first `<release>`: `version="1.2.1"` with the tag day and `<p>Settings opens only after a service rejects the key twice; invalid-key errors name the status and code; a 403 without JSON is a plain error.</p>`; screenshot URL `/v1.2.1/`; manifest `tag: v1.2.1`.

- [ ] **Step 4: Full check and commit (no tag)**

Run: `python3 test_debrid_api.py | tail -1 && appstreamcli validate --no-net --pedantic io.github.xb00tt.debrid-applet.metainfo.xml && packaging/build-deb.sh && python3 debrid_applet.py --version`
Expected: `93 passed`, clean validation, the deb path, `debrid-applet 1.2.1`.

```bash
git add docs/MANUAL.md debrid_applet.py README.md io.github.xb00tt.debrid-applet.metainfo.xml packaging/io.github.xb00tt.debrid-applet.yml
git commit -m "Release 1.2.1"
```

Tag and push are the maintainer's.
