import re
import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from app.cipher import caesar
from app.db import get_connection, init_db

app = FastAPI(title="Sample managed project")

init_db()

_bearer = HTTPBearer(auto_error=False)


class RegisterRequest(BaseModel):
    username: str
    pin: str

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        if not (3 <= len(v) <= 30):
            raise ValueError("username must be 3-30 characters")
        if not re.fullmatch(r"[A-Za-z0-9_]+", v):
            raise ValueError("username must contain only letters, digits, and underscores")
        return v

    @field_validator("pin")
    @classmethod
    def pin_valid(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9]{5}", v):
            raise ValueError("pin must be exactly 5 alphanumeric characters")
        return v


class LoginRequest(BaseModel):
    username: str
    pin: str


class EntryRequest(BaseModel):
    body: str
    shift: int

    @field_validator("body")
    @classmethod
    def body_valid(cls, v: str) -> str:
        if not (1 <= len(v) <= 10000):
            raise ValueError("body must be 1-10000 characters")
        return v

    @field_validator("shift")
    @classmethod
    def shift_valid(cls, v: int) -> int:
        if not (1 <= v <= 25):
            raise ValueError("shift must be 1-25")
        return v


def _current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> int:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    conn = get_connection()
    row = conn.execute(
        "SELECT user_id FROM sessions WHERE token = ?", (credentials.credentials,)
    ).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )
    return int(row["user_id"])


@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest) -> dict[str, object]:
    conn = get_connection()
    try:
        if conn.execute("SELECT id FROM users WHERE username = ?", (req.username,)).fetchone():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Username already taken"
            )
        cursor = conn.execute(
            "INSERT INTO users (username, pin) VALUES (?, ?)",
            (req.username, req.pin),
        )
        conn.commit()
        user_id = cursor.lastrowid
    finally:
        conn.close()
    return {"id": user_id, "username": req.username}


@app.post("/auth/login")
def login(req: LoginRequest) -> dict[str, str]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ? AND pin = ?",
            (req.username, req.pin),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
            )
        token = secrets.token_hex(16)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, row["id"], now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"token": token}


@app.post("/auth/logout")
def logout(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict[str, bool]:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    token = credentials.credentials
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM sessions WHERE token = ?", (token,)).fetchone() is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
            )
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.post("/entries", status_code=status.HTTP_201_CREATED)
def create_entry(
    req: EntryRequest,
    user_id: Annotated[int, Depends(_current_user)],
) -> dict[str, object]:
    ciphertext = caesar(req.body, req.shift)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO entries (user_id, ciphertext, shift, created_at) VALUES (?, ?, ?, ?)",
            (user_id, ciphertext, req.shift, now),
        )
        conn.commit()
        entry_id = cursor.lastrowid
    finally:
        conn.close()
    return {"id": entry_id, "ciphertext": ciphertext, "shift": req.shift, "created_at": now}


@app.get("/entries")
def list_entries(user_id: Annotated[int, Depends(_current_user)]) -> list[dict[str, object]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, ciphertext, shift, created_at FROM entries "
        "WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "ciphertext": r["ciphertext"],
            "shift": r["shift"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get("/entries/{entry_id}")
def get_entry(
    entry_id: int,
    user_id: Annotated[int, Depends(_current_user)],
    decrypt: bool = False,
) -> dict[str, object]:
    conn = get_connection()
    row = conn.execute(
        "SELECT id, user_id, ciphertext, shift, created_at FROM entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
    if row["user_id"] != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    plaintext = caesar(row["ciphertext"], 26 - row["shift"]) if decrypt else None
    return {
        "id": row["id"],
        "ciphertext": row["ciphertext"],
        "plaintext": plaintext,
        "shift": row["shift"],
        "created_at": row["created_at"],
    }


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
