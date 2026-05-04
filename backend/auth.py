"""
Smart Energy Hub — Authentification (Sprint 10)

Gestion des utilisateurs (admin / user), hash bcrypt, sessions JWT en cookie httpOnly.

Tables créées dans la DB principale (via _migrate dans database.py v5).

Endpoints exposés via auth_router :
  POST /api/auth/login     {username, password} → cookie JWT
  POST /api/auth/logout    → efface cookie
  GET  /api/auth/me        → infos user courant
  GET  /api/auth/users     (admin)  → liste users
  POST /api/auth/users     (admin)  → créer user
  DELETE /api/auth/users/{id}  (admin) → supprimer user
  POST /api/auth/password  → changer son propre mot de passe

Le secret JWT est stocké dans /data/jwt_secret (généré au premier démarrage).

Convention :
  - Si AUCUN user n'existe → mode "first_boot" : tous les endpoints publics
    (le wizard d'onboarding s'occupera de créer l'admin initial).
  - Dès qu'un admin existe → auth requise sur tous les endpoints sauf
    /api/auth/login, /health, /api/setup/* (tant que first_boot=true).
"""
from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import bcrypt
import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

JWT_ALGO = "HS256"
JWT_TTL_SECONDS = 7 * 24 * 3600  # 7 jours
COOKIE_NAME = "seh_session"
SECRET_PATH = os.environ.get("JWT_SECRET_PATH", "/data/jwt_secret")

ROLE_ADMIN = "admin"
ROLE_USER = "user"
VALID_ROLES = {ROLE_ADMIN, ROLE_USER}


# ═══════════════════════════════════════════════════════════════════════════
#  Secret JWT — généré une seule fois et persisté
# ═══════════════════════════════════════════════════════════════════════════

def _load_or_create_secret(path: str = SECRET_PATH) -> str:
    p = Path(path)
    if p.exists():
        try:
            secret = p.read_text().strip()
            if len(secret) >= 32:
                return secret
        except Exception as e:
            logger.warning("Lecture secret JWT échouée: %s — régénération", e)
    # Génère un nouveau secret 64 caractères hex
    secret = secrets.token_hex(32)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secret)
        os.chmod(path, 0o600)
        logger.info("Secret JWT généré → %s", path)
    except Exception as e:
        logger.error("Impossible de persister le secret JWT (%s) — il sera régénéré au prochain démarrage", e)
    return secret


# ═══════════════════════════════════════════════════════════════════════════
#  Modèles Pydantic
# ═══════════════════════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=256)
    role: str = Field(default=ROLE_USER)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=256)


@dataclass
class User:
    id: int
    username: str
    role: str
    created_at: int
    last_login: Optional[int]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
            "last_login": self.last_login,
        }


# ═══════════════════════════════════════════════════════════════════════════
#  AuthManager — encapsule l'accès DB et la logique
# ═══════════════════════════════════════════════════════════════════════════

class AuthManager:
    """
    Gère les utilisateurs et les sessions JWT.

    Utilise la même connexion SQLite que EnergyDatabase (passée en paramètre).
    Les tables sont créées par la migration v5 de database.py.
    """

    def __init__(self, conn: sqlite3.Connection, secret: Optional[str] = None):
        self._conn = conn
        self._secret = secret or _load_or_create_secret()

    # ── Hash ────────────────────────────────────────────────────────────────

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
        except Exception:
            return False

    # ── CRUD users ──────────────────────────────────────────────────────────

    def count_users(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(row[0]) if row else 0

    def count_admins(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM users WHERE role=?", (ROLE_ADMIN,)).fetchone()
        return int(row[0]) if row else 0

    def has_any_user(self) -> bool:
        return self.count_users() > 0

    def list_users(self) -> list[User]:
        rows = self._conn.execute(
            "SELECT id, username, role, created_at, last_login FROM users ORDER BY id ASC"
        ).fetchall()
        return [User(id=r[0], username=r[1], role=r[2], created_at=r[3], last_login=r[4]) for r in rows]

    def get_user(self, user_id: int) -> Optional[User]:
        r = self._conn.execute(
            "SELECT id, username, role, created_at, last_login FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if not r:
            return None
        return User(id=r[0], username=r[1], role=r[2], created_at=r[3], last_login=r[4])

    def get_user_by_username(self, username: str) -> Optional[User]:
        r = self._conn.execute(
            "SELECT id, username, role, created_at, last_login FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if not r:
            return None
        return User(id=r[0], username=r[1], role=r[2], created_at=r[3], last_login=r[4])

    def create_user(self, username: str, password: str, role: str = ROLE_USER) -> User:
        username = username.strip()
        if not username:
            raise ValueError("username vide")
        if role not in VALID_ROLES:
            raise ValueError(f"role invalide: {role}")
        if len(password) < 8:
            raise ValueError("mot de passe trop court (8 caractères minimum)")
        # Unicité
        if self.get_user_by_username(username):
            raise ValueError(f"username déjà pris: {username}")
        now = int(time.time())
        cur = self._conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, self.hash_password(password), role, now),
        )
        self._conn.commit()
        user_id = cur.lastrowid
        logger.info("User créé: %s (role=%s, id=%d)", username, role, user_id)
        return User(id=user_id, username=username, role=role, created_at=now, last_login=None)

    def delete_user(self, user_id: int) -> bool:
        # Empêche de supprimer le dernier admin
        u = self.get_user(user_id)
        if not u:
            return False
        if u.role == ROLE_ADMIN and self.count_admins() <= 1:
            raise ValueError("Impossible de supprimer le dernier admin")
        self._conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        self._conn.commit()
        logger.info("User supprimé: %s (id=%d)", u.username, user_id)
        return True

    def change_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        r = self._conn.execute(
            "SELECT password_hash FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not r:
            return False
        if not self.verify_password(current_password, r[0]):
            raise ValueError("Mot de passe actuel incorrect")
        if len(new_password) < 8:
            raise ValueError("Nouveau mot de passe trop court (8 caractères minimum)")
        self._conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (self.hash_password(new_password), user_id),
        )
        self._conn.commit()
        return True

    def reset_password(self, user_id: int, new_password: str) -> bool:
        """Reset sans connaître le mot de passe actuel — réservé à un admin sur un autre user."""
        if len(new_password) < 8:
            raise ValueError("Nouveau mot de passe trop court (8 caractères minimum)")
        cur = self._conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (self.hash_password(new_password), user_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ── Authentification ────────────────────────────────────────────────────

    def authenticate(self, username: str, password: str) -> Optional[User]:
        r = self._conn.execute(
            "SELECT id, username, password_hash, role, created_at FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if not r:
            return None
        if not self.verify_password(password, r[2]):
            return None
        # Maj last_login
        now = int(time.time())
        self._conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, r[0]))
        self._conn.commit()
        return User(id=r[0], username=r[1], role=r[3], created_at=r[4], last_login=now)

    # ── JWT ─────────────────────────────────────────────────────────────────

    def create_token(self, user: User) -> str:
        now = int(time.time())
        payload = {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "iat": now,
            "exp": now + JWT_TTL_SECONDS,
        }
        return jwt.encode(payload, self._secret, algorithm=JWT_ALGO)

    def verify_token(self, token: str) -> Optional[dict]:
        try:
            return jwt.decode(token, self._secret, algorithms=[JWT_ALGO])
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None

    def user_from_token(self, token: str) -> Optional[User]:
        payload = self.verify_token(token)
        if not payload:
            return None
        try:
            uid = int(payload["sub"])
        except (KeyError, ValueError):
            return None
        return self.get_user(uid)


# ═══════════════════════════════════════════════════════════════════════════
#  Singleton global + dépendances FastAPI
# ═══════════════════════════════════════════════════════════════════════════

_auth: Optional[AuthManager] = None


def init_auth(conn: sqlite3.Connection) -> AuthManager:
    global _auth
    _auth = AuthManager(conn)
    return _auth


def get_auth() -> AuthManager:
    if _auth is None:
        raise RuntimeError("AuthManager non initialisé — appeler init_auth() d'abord")
    return _auth


def get_current_user(seh_session: Optional[str] = Cookie(default=None)) -> User:
    """Dépendance FastAPI : lève 401 si pas de session valide."""
    auth = get_auth()
    if not seh_session:
        raise HTTPException(status_code=401, detail="Non authentifié")
    user = auth.user_from_token(seh_session)
    if not user:
        raise HTTPException(status_code=401, detail="Session invalide ou expirée")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    """Dépendance FastAPI : lève 403 si pas admin."""
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Privilèges admin requis")
    return user


def get_optional_user(seh_session: Optional[str] = Cookie(default=None)) -> Optional[User]:
    """Dépendance FastAPI : retourne None si pas de session, ne lève jamais."""
    if not seh_session or _auth is None:
        return None
    return _auth.user_from_token(seh_session)


# ═══════════════════════════════════════════════════════════════════════════
#  Router FastAPI
# ═══════════════════════════════════════════════════════════════════════════

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=JWT_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,  # Pas HTTPS en local — TODO: passer à True en prod via reverse proxy
        path="/",
    )


def _clear_session_cookie(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")


@auth_router.post("/login")
async def login(req: LoginRequest, response: Response):
    auth = get_auth()
    user = auth.authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Identifiants invalides")
    token = auth.create_token(user)
    _set_session_cookie(response, token)
    return {"ok": True, "user": user.to_dict()}


@auth_router.post("/logout")
async def logout(response: Response):
    _clear_session_cookie(response)
    return {"ok": True}


@auth_router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {"user": user.to_dict()}


@auth_router.get("/users")
async def list_users(_admin: User = Depends(get_current_admin)):
    auth = get_auth()
    return {"users": [u.to_dict() for u in auth.list_users()]}


@auth_router.post("/users")
async def create_user(req: CreateUserRequest, _admin: User = Depends(get_current_admin)):
    auth = get_auth()
    try:
        u = auth.create_user(req.username, req.password, req.role)
        return {"ok": True, "user": u.to_dict()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@auth_router.delete("/users/{user_id}")
async def delete_user(user_id: int, _admin: User = Depends(get_current_admin)):
    auth = get_auth()
    try:
        ok = auth.delete_user(user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@auth_router.post("/password")
async def change_my_password(req: ChangePasswordRequest, user: User = Depends(get_current_user)):
    auth = get_auth()
    try:
        auth.change_password(user.id, req.current_password, req.new_password)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
