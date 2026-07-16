from fastapi.testclient import TestClient

from app.cipher import caesar
from app.main import app

client = TestClient(app)


# Helper
def register_and_login(username: str, pin: str) -> str:
    client.post("/auth/register", json={"username": username, "pin": pin})
    resp = client.post("/auth/login", json={"username": username, "pin": pin})
    return resp.json()["token"]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# Frontend
def test_root_serves_html():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# Auth
def test_register_success():
    resp = client.post("/auth/register", json={"username": "user_reg1", "pin": "Ab1Cd"})
    assert resp.status_code == 201
    assert resp.json()["username"] == "user_reg1"


def test_register_duplicate():
    client.post("/auth/register", json={"username": "dupuser", "pin": "Ab1Cd"})
    resp = client.post("/auth/register", json={"username": "dupuser", "pin": "Ab1Cd"})
    assert resp.status_code == 409


def test_login_success():
    client.post("/auth/register", json={"username": "loginok", "pin": "Ab1Cd"})
    resp = client.post("/auth/login", json={"username": "loginok", "pin": "Ab1Cd"})
    assert resp.status_code == 200
    assert "token" in resp.json()


def test_login_wrong_pin():
    client.post("/auth/register", json={"username": "loginbad", "pin": "Ab1Cd"})
    resp = client.post("/auth/login", json={"username": "loginbad", "pin": "XXXXX"})
    assert resp.status_code == 401


# Entries
def test_create_entry_no_auth():
    resp = client.post("/entries", json={"body": "hello", "shift": 3})
    assert resp.status_code == 401


def test_create_entry_with_token():
    token = register_and_login("entryuser1", "Ab1Cd")
    resp = client.post(
        "/entries", json={"body": "hello world", "shift": 3}, headers=auth_headers(token)
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["ciphertext"] != "hello world"


def test_list_entries():
    token = register_and_login("listuser1", "Ab1Cd")
    client.post("/entries", json={"body": "my entry", "shift": 5}, headers=auth_headers(token))
    resp = client.get("/entries", headers=auth_headers(token))
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) >= 1


def test_get_entry_no_decrypt():
    token = register_and_login("nodecrypt1", "Ab1Cd")
    create = client.post(
        "/entries", json={"body": "secret", "shift": 4}, headers=auth_headers(token)
    )
    entry_id = create.json()["id"]
    resp = client.get(f"/entries/{entry_id}", headers=auth_headers(token))
    assert resp.status_code == 200
    assert resp.json()["plaintext"] is None


def test_get_entry_with_decrypt():
    token = register_and_login("decryptok1", "Ab1Cd")
    body = "Had a wonderful day"
    create = client.post("/entries", json={"body": body, "shift": 7}, headers=auth_headers(token))
    entry_id = create.json()["id"]
    resp = client.get(f"/entries/{entry_id}?decrypt=true", headers=auth_headers(token))
    assert resp.status_code == 200
    assert resp.json()["plaintext"] == body


def test_get_entry_other_user():
    token1 = register_and_login("owner_user1", "Ab1Cd")
    token2 = register_and_login("other_user1", "Ab1Cd")
    create = client.post(
        "/entries", json={"body": "private", "shift": 2}, headers=auth_headers(token1)
    )
    entry_id = create.json()["id"]
    resp = client.get(f"/entries/{entry_id}", headers=auth_headers(token2))
    assert resp.status_code == 403


def test_logout():
    token = register_and_login("logoutuser1", "Ab1Cd")
    resp = client.post("/auth/logout", headers=auth_headers(token))
    assert resp.status_code == 200
    resp2 = client.get("/entries", headers=auth_headers(token))
    assert resp2.status_code == 401


# Cipher unit tests
def test_caesar_encrypt():
    assert caesar("Hello", 3) == "Khoor"


def test_caesar_decrypt():
    assert caesar("Khoor", 23) == "Hello"


def test_caesar_non_letters():
    assert caesar("Hello, World!", 1) == "Ifmmp, Xpsme!"


def test_caesar_wrap():
    assert caesar("xyz", 3) == "abc"
