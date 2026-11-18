"""Accounts and browser sessions for the simulated payment workspace."""

from collections import deque
from hashlib import sha256, scrypt
from hmac import compare_digest
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import secrets
from threading import Lock
import time

from payments import PAYER, PaymentError


COOKIE = "upi_demo_session"
SESSION_SECONDS = 8 * 60 * 60
REMEMBER_SECONDS = 365 * 24 * 60 * 60
ACCOUNTS = {
    "sender": ("sender", PAYER, "Demo payer"),
    "bank": ("bank", None, "Bank reviewer"),
    "canteen": ("receiver", "canteen", "Campus Canteen"),
    "rent": ("receiver", "rent", "Greenview Rentals"),
    "new-recipient": ("receiver", "new-recipient", "New recipient"),
}


def load_demo_passwords(signins):
    signins = Path(signins)
    if not signins.exists():
        signins.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(signins, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            json.dump({name: secrets.token_urlsafe(16) for name in ACCOUNTS}, file, indent=2)
            file.write("\n")
    passwords = json.loads(signins.read_text(encoding="utf-8"))
    if not isinstance(passwords, dict) or set(passwords) != ACCOUNTS.keys() or any(
        not isinstance(value, str) or not 12 <= len(value) <= 128 for value in passwords.values()
    ) or len(set(passwords.values())) != len(ACCOUNTS):
        raise ValueError("Demo sign-ins require a unique password of at least 12 characters for each account.")
    return passwords


class DemoAuth:
    def __init__(self, store, signins):
        self.store = store
        self.failures = deque()
        self.lock = Lock()
        passwords = load_demo_passwords(signins)
        self.salt = secrets.token_bytes(16)
        self.passwords = {name: self.hash_password(password) for name, password in passwords.items()}
        self.dummy_hash = self.hash_password(secrets.token_urlsafe(16))
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS demo_sessions (
                digest TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at REAL NOT NULL, operator INTEGER NOT NULL DEFAULT 0
            )""")
            if not any(column["name"] == "operator" for column in db.execute("PRAGMA table_info(demo_sessions)")):
                db.execute("ALTER TABLE demo_sessions ADD COLUMN operator INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE demo_sessions SET operator=1 WHERE username='bank'")
        self.purge()

    def purge(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM demo_sessions WHERE expires_at <= ?", (time.time(),))

    def hash_password(self, password):
        return scrypt(password.encode(), salt=self.salt, n=2**14, r=8, p=1, dklen=32)

    def sign_in(self, username, password, previous_cookie, remember=False):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 128:
            raise PaymentError("Check your demo username and password.", 401)
        username = username.strip().lower()
        with self.lock:
            now = time.monotonic()
            while self.failures and self.failures[0] <= now - 60:
                self.failures.popleft()
            if len(self.failures) >= 30:
                raise PaymentError("Too many sign-in attempts. Try again in a minute.", 429)
            expected = self.passwords.get(username, self.dummy_hash)
            if not compare_digest(self.hash_password(password), expected) or username not in ACCOUNTS:
                self.failures.append(now)
                raise PaymentError("Check your demo username and password.", 401)

        token = secrets.token_urlsafe(32)
        previous = self.cookie_digest(previous_cookie)
        with self.store.transaction() as db:
            if previous:
                db.execute("DELETE FROM demo_sessions WHERE digest = ?", (previous,))
            lifetime = REMEMBER_SECONDS if remember else SESSION_SECONDS
            db.execute("INSERT INTO demo_sessions(digest, username, expires_at, operator) VALUES (?, ?, ?, ?)",
                       (sha256(token.encode()).hexdigest(), username, time.time() + lifetime, username == "bank"))
        return token, {**self.identity(username), "can_switch": username == "bank"}

    @staticmethod
    def identity(username):
        role, account, name = ACCOUNTS[username]
        return {"username": username, "role": role, "account": account, "name": name}

    @staticmethod
    def cookie_digest(header):
        if not header:
            return None
        try:
            cookie = SimpleCookie(header).get(COOKIE)
        except CookieError:
            return None
        if cookie is None or len(cookie.value) != 43:
            return None
        return sha256(cookie.value.encode()).hexdigest()

    def session(self, header):
        digest = self.cookie_digest(header)
        if digest is None:
            return None
        with self.store.connection() as db:
            row = db.execute("SELECT username, operator FROM demo_sessions WHERE digest = ? AND expires_at > ?",
                             (digest, time.time())).fetchone()
        if row and row["username"] in ACCOUNTS:
            return {**self.identity(row["username"]), "can_switch": bool(row["operator"])}
        return None

    def switch(self, header, username):
        if not isinstance(username, str) or username not in ACCOUNTS:
            raise PaymentError("Choose a demo account.")
        digest = self.cookie_digest(header)
        with self.store.transaction() as db:
            result = db.execute("""UPDATE demo_sessions SET username = ?
                WHERE digest = ? AND expires_at > ? AND operator = 1""", (username, digest, time.time()))
            if not result.rowcount:
                raise PaymentError("Sign in as the bank operator to switch roles.", 403)
        return {**self.identity(username), "can_switch": True}

    def sign_out(self, header):
        digest = self.cookie_digest(header)
        if digest:
            with self.store.connection() as db:
                db.execute("DELETE FROM demo_sessions WHERE digest = ?", (digest,))

    @staticmethod
    def cookie(token="", remember=False):
        lifetime = f"; Max-Age={REMEMBER_SECONDS}" if token and remember else "" if token else "; Max-Age=0"
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict{lifetime}"
