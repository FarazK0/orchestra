# Project Specification: Caesar Cipher Online Diary

## Overview

Build a small web application that lets users keep a private diary. Each user registers with a
username and a 5-character alphanumeric PIN. When writing an entry the user picks a Caesar cipher
shift (1-25); the app stores the entry encrypted and can decrypt it on demand when the user
provides the same shift. The cipher is intentionally simple — this is a personal privacy layer,
not cryptographic security.

---

## Tech stack

Extend the existing FastAPI app in `app/main.py`. Use:

- **FastAPI** for all routes (already a dependency)
- **SQLite** via the standard-library `sqlite3` module for persistence (`diary.db` at the repo
  root); no ORM, raw SQL is fine for this scope
- **Plain bearer tokens** for sessions: on login, generate a random 32-character hex token, store
  it in a `sessions` table, return it to the client; the client sends it as `Authorization: Bearer
  <token>` on every subsequent request
- **No external dependencies** beyond what is already installed

---

## Caesar cipher

```
encrypt(text, shift):
    for each character c in text:
        if c is a letter: rotate it by shift within its case (a-z or A-Z), wrapping at 26
        else: leave it unchanged

decrypt(ciphertext, shift):
    encrypt(ciphertext, 26 - shift)   # rotating back is just encrypting with the complement
```

Implement this as a pure function `caesar(text: str, shift: int) -> str` in `app/cipher.py`.

---

## Database schema

Create the database and tables on startup if they do not exist.

```sql
CREATE TABLE IF NOT EXISTS users (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT    NOT NULL UNIQUE,
    pin      TEXT    NOT NULL          -- store as plain text (demo only)
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT    NOT NULL        -- ISO-8601 UTC timestamp
);

CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    ciphertext   TEXT    NOT NULL,     -- Caesar-encrypted body
    shift        INTEGER NOT NULL,     -- shift used; stored so owner can always decrypt
    created_at   TEXT    NOT NULL      -- ISO-8601 UTC timestamp
);
```

---

## API endpoints

All endpoints return JSON. Error responses use `{"detail": "<message>"}`.

### POST /auth/register

Register a new user.

**Request body**
```json
{ "username": "alice", "pin": "A1b2C" }
```

**Validation**
- `username`: 3-30 characters, letters/digits/underscores only
- `pin`: exactly 5 alphanumeric characters

**Responses**
- `201` `{"id": 1, "username": "alice"}`
- `400` if validation fails
- `409` if username already taken

---

### POST /auth/login

Authenticate and receive a session token.

**Request body**
```json
{ "username": "alice", "pin": "A1b2C" }
```

**Responses**
- `200` `{"token": "<32-char hex>"}`
- `401` if credentials are wrong

---

### POST /entries

Create a new diary entry. Requires `Authorization: Bearer <token>`.

**Request body**
```json
{ "body": "Had a wonderful day at the park.", "shift": 7 }
```

**Validation**
- `body`: 1-10000 characters
- `shift`: integer 1-25

The server encrypts `body` with the given `shift` and stores the ciphertext.

**Response**
- `201`
```json
{
  "id": 1,
  "ciphertext": "Ohk h dvuklymbssha khf ha aol whyr.",
  "shift": 7,
  "created_at": "2026-07-15T10:00:00Z"
}
```
- `401` if token missing or invalid
- `400` if validation fails

---

### GET /entries

List all entries for the authenticated user, newest first. Requires `Authorization: Bearer
<token>`.

**Response**
- `200`
```json
[
  {
    "id": 1,
    "ciphertext": "Ohk h dvuklymbssha khf ha aol whyr.",
    "shift": 7,
    "created_at": "2026-07-15T10:00:00Z"
  }
]
```
Returns an empty list `[]` if the user has no entries.

---

### GET /entries/{entry_id}

Fetch a single entry. Requires `Authorization: Bearer <token>`.

Include `?decrypt=true` to receive the plaintext alongside the ciphertext.

**Response**
- `200`
```json
{
  "id": 1,
  "ciphertext": "Ohk h dvuklymbssha khf ha aol whyr.",
  "plaintext": "Had a wonderful day at the park.",
  "shift": 7,
  "created_at": "2026-07-15T10:00:00Z"
}
```
`plaintext` is `null` when `decrypt` is omitted or false.
- `401` if token invalid
- `403` if the entry belongs to a different user
- `404` if entry does not exist

---

### POST /auth/logout

Invalidate the current session token. Requires `Authorization: Bearer <token>`.

**Response** `200` `{"ok": true}`

---

## Frontend

Create a single-page app at `frontend/index.html` using vanilla HTML + JavaScript (no build
step, no frameworks). The page should:

1. **Login / register screen** (shown when no token is stored in `localStorage`):
   - Username input
   - PIN input (type=password, maxlength=5)
   - Two buttons: "Log in" and "Register"
   - Show an inline error message on failure

2. **Diary screen** (shown after successful login):
   - Header: "Hello, {username}" with a "Log out" link
   - Textarea + shift selector (a `<select>` for 1-25) + "Add entry" button
   - Entry list below: for each entry show the creation date, a "Decrypt" button, and the
     ciphertext (or plaintext once decrypted)
   - Decrypting calls `GET /entries/{id}?decrypt=true` and replaces the ciphertext inline

The page must work when served as a static file by FastAPI:

```python
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
```

Add this mount at the end of `app/main.py` (after all API routes are registered) so `/` serves
`frontend/index.html` while `/auth/...` and `/entries` continue to work as API routes.

---

## Tests

Add tests in `tests/test_app.py` using `httpx.AsyncClient` + `pytest-asyncio` (already installed
or use `TestClient` from `starlette`):

1. Register a user → 201
2. Register same username again → 409
3. Login with correct PIN → 200, token in response
4. Login with wrong PIN → 401
5. Create entry without auth → 401
6. Create entry with valid token → 201, ciphertext differs from body
7. List entries → contains the created entry
8. Get single entry without `?decrypt=true` → plaintext is null
9. Get single entry with `?decrypt=true` → plaintext matches original body
10. Get entry belonging to another user → 403
11. Logout → 200; subsequent request with that token → 401

Also add unit tests for `app/cipher.py`:

12. `caesar("Hello", 3)` == `"Khoor"`
13. `caesar("Khoor", 23)` == `"Hello"` (26-3 = 23 decrypts)
14. Non-letter characters are unchanged: `caesar("Hello, World!", 1)` == `"Ifmmp, Xpsme!"`
15. Wrap-around: `caesar("xyz", 3)` == `"abc"`

---

## Acceptance criteria

- `GET /` returns the diary HTML page (or a redirect to `index.html`)
- All 15 tests pass under `pytest`
- `ruff check .` passes with no errors
- Register → login → create entry → list entries → decrypt entry works end-to-end via the
  frontend without touching the browser console
- An entry decrypted with a wrong shift returns garbled text (no server-side error; the cipher
  doesn't know if the shift is "right")
