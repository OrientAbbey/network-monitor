"""
Authentification des endpoints de contrôle (§37).

- Utilisateurs : `NM_USERS`, un objet JSON `{"alice": "<hash>"}`. Les mots de passe ne sont
  jamais stockés : seul un hachage scrypt salé l'est. Générer un hachage :
  `python -m network_monitor.app.auth hash`.
- Session : `POST /auth/login` renvoie un jeton signé (HMAC-SHA256, clé `NM_AUTH_SECRET`) qui
  expire au bout de `NM_TOKEN_TTL_MINUTES` (60 par défaut). Sans `NM_AUTH_SECRET`, une clé
  aléatoire est tirée au démarrage : les sessions ne survivent pas à un redémarrage.
- Machine : `NM_API_TOKEN` reste accepté tel quel (scripts, supervision) ; il n'a pas d'expiration.
- Essais : 5 échecs en 5 minutes pour un même (utilisateur, adresse) bloquent la connexion.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_MAXMEM = 64 * 1024 * 1024
_MAX_FAILURES, _FAILURE_WINDOW = 5, timedelta(minutes=5)

_b64 = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")
_unb64 = lambda text: base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_MAXMEM, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=32
        )
        return hmac.compare_digest(digest, _unb64(expected))
    except (ValueError, TypeError):
        return False


# Hachage jeté à la place de celui d'un utilisateur inconnu : la durée de la réponse ne
# révèle pas si le nom d'utilisateur existe.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(8))


def issue_token(username: str, secret: bytes, ttl: timedelta, now: datetime | None = None) -> str:
    expires = int(((now or utcnow()) + ttl).timestamp())
    payload = _b64(json.dumps({"sub": username, "exp": expires}, separators=(",", ":")).encode())
    return f"{payload}.{_b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())}"


def verify_token(token: str, secret: bytes, now: datetime | None = None) -> str | None:
    """Nom d'utilisateur si le jeton est authentique et non expiré, sinon None."""
    try:
        payload, signature = token.split(".")
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(signature), expected):
            return None
        claims = json.loads(_unb64(payload))
        if int(claims["exp"]) <= int((now or utcnow()).timestamp()):
            return None
        return str(claims["sub"])
    except (ValueError, KeyError, TypeError):
        return None


class LoginThrottle:
    """Limite les essais de connexion échoués par (utilisateur, adresse)."""

    def __init__(self) -> None:
        self._failures: dict[tuple[str, str], deque[datetime]] = defaultdict(deque)

    def _recent(self, key: tuple[str, str], now: datetime) -> deque[datetime]:
        failures = self._failures[key]
        while failures and now - failures[0] > _FAILURE_WINDOW:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)
        return failures

    def blocked(self, username: str, client: str, now: datetime | None = None) -> bool:
        return len(self._recent((username, client), now or utcnow())) >= _MAX_FAILURES

    def record_failure(self, username: str, client: str, now: datetime | None = None) -> None:
        self._failures[(username, client)].append(now or utcnow())

    def reset(self, username: str, client: str) -> None:
        self._failures.pop((username, client), None)


@dataclass(frozen=True)
class AuthSettings:
    users: dict[str, str]
    secret: bytes
    ttl: timedelta
    static_token: str | None

    @property
    def configured(self) -> bool:
        return bool(self.users or self.static_token)


_fallback_secret = secrets.token_bytes(32)


def load_settings() -> AuthSettings:
    """Lit l'environnement à chaque appel : la configuration change sans redémarrer les tests."""
    users = json.loads(os.environ.get("NM_USERS") or "{}")
    secret = os.environ.get("NM_AUTH_SECRET")
    return AuthSettings(
        users={str(k): str(v) for k, v in users.items()},
        secret=secret.encode() if secret else _fallback_secret,
        ttl=timedelta(minutes=float(os.environ.get("NM_TOKEN_TTL_MINUTES", "60"))),
        static_token=os.environ.get("NM_API_TOKEN") or None,
    )


def authenticate(settings: AuthSettings, username: str, password: str) -> bool:
    stored = settings.users.get(username)
    ok = verify_password(password, stored or _DUMMY_HASH)
    return ok and stored is not None


def subject_for(settings: AuthSettings, bearer: str, now: datetime | None = None) -> str | None:
    """Identité derrière un jeton porteur : jeton machine, ou jeton de session valide."""
    if settings.static_token and hmac.compare_digest(bearer.encode(), settings.static_token.encode()):
        return "api-token"
    return verify_token(bearer, settings.secret, now)


if __name__ == "__main__":  # pragma: no cover
    if sys.argv[1:] != ["hash"]:
        sys.exit("usage : python -m network_monitor.app.auth hash")
    password = getpass.getpass("Mot de passe : ")
    if password != getpass.getpass("Confirmer : "):
        sys.exit("Les mots de passe diffèrent.")
    print(hash_password(password))
