"""
Smart Energy Hub — Wizard d'onboarding (Sprint 10)

Endpoints exposés tant que first_boot=true (mode bootstrap).
Une fois le wizard terminé (POST /api/setup/complete), first_boot passe à
false et ces endpoints deviennent inaccessibles aux non-admins.

Endpoints :
  GET  /api/setup/status          → état du wizard (first_boot, étapes complétées)
  POST /api/setup/scan_network    → lance un scan réseau pour détecter modules
  POST /api/setup/test_connection → teste un module avant validation
  POST /api/setup/complete        → finalise wizard : crée admin, sauvegarde config, sort de first_boot
  POST /api/setup/reopen          → (admin) réouvre le wizard pour ajouter un module
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import get_auth, get_current_admin, ROLE_ADMIN

logger = logging.getLogger(__name__)

setup_router = APIRouter(prefix="/api/setup", tags=["setup"])

# Référence injectée depuis main.py
_db = None
_cfg = None
_scanner = None  # NetworkScanner (commit 3)


def init_setup(db, cfg, scanner=None):
    """À appeler depuis main.py lifespan."""
    global _db, _cfg, _scanner
    _db = db
    _cfg = cfg
    _scanner = scanner


# ═══════════════════════════════════════════════════════════════════════════
#  Modèles Pydantic
# ═══════════════════════════════════════════════════════════════════════════

class ScanRequest(BaseModel):
    cidr: Optional[str] = Field(default=None, description="ex: 192.168.1.0/24 — auto-détecté si absent")
    timeout: float = Field(default=1.0, ge=0.1, le=5.0)


class TestConnectionRequest(BaseModel):
    module_type: str  # voltronic | victron | solax | jkbms | pylontech
    host: str
    port: int = 502
    extra: dict = Field(default_factory=dict)  # ex: {unit_id: 1, num_batteries: 4}


class CompleteWizardRequest(BaseModel):
    admin_username: str = Field(..., min_length=3, max_length=64)
    admin_password: str = Field(..., min_length=8, max_length=256)
    config: dict = Field(default_factory=dict, description="Config complète à fusionner")
    enabled_modules: list[str] = Field(default_factory=list, description="Modules cochés au wizard")


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _is_first_boot() -> bool:
    if not _db:
        return True
    return _db.is_first_boot()


def _check_setup_allowed(request: Request):
    """
    Le wizard est accessible :
    - en mode bootstrap (first_boot=true) → tout le monde peut accéder
    - sinon → admin seulement
    """
    if _is_first_boot():
        return  # OK, mode bootstrap
    # Sinon, exige admin
    auth = get_auth()
    token = request.cookies.get("seh_session")
    if not token:
        raise HTTPException(status_code=401, detail="Non authentifié")
    user = auth.user_from_token(token)
    if not user or user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Privilèges admin requis (wizard fermé après onboarding)")


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@setup_router.get("/status")
async def setup_status():
    """
    Retourne l'état du wizard. Toujours accessible (utilisé par le frontend
    pour décider s'il faut afficher /setup ou /).
    """
    if not _db:
        return {"first_boot": True, "users_count": 0, "ready": False}
    auth = get_auth()
    return {
        "first_boot": _db.is_first_boot(),
        "users_count": auth.count_users(),
        "admins_count": auth.count_admins(),
        "ready": _db is not None and _cfg is not None,
        "available_modules": [
            {"id": "voltronic", "name": "Onduleur Voltronic / Axpert", "icon": "⚡"},
            {"id": "victron", "name": "Écosystème Victron (Cerbo GX)", "icon": "🔋"},
            {"id": "solax", "name": "Onduleur Solax (X1/X3 Hybrid)", "icon": "☀️"},
            {"id": "pylontech", "name": "BMS Pylontech US2000B", "icon": "🔋"},
            {"id": "jkbms", "name": "BMS JK-BMS (LiFePO4)", "icon": "🔋"},
            {"id": "finance", "name": "Module Finance (tarifs EDF)", "icon": "💰"},
            {"id": "forecast", "name": "Prévisions solaires (Open-Meteo)", "icon": "🔮"},
            {"id": "leaf", "name": "Optimiseur Nissan Leaf", "icon": "🚗"},
            {"id": "alerts", "name": "Alertes Telegram", "icon": "🚨"},
            {"id": "mqtt", "name": "MQTT / Home Assistant", "icon": "📡"},
        ],
    }


@setup_router.post("/scan_network")
async def setup_scan_network(req: ScanRequest, request: Request):
    _check_setup_allowed(request)
    if _scanner is None:
        raise HTTPException(status_code=503, detail="Scanner réseau non disponible")
    try:
        results = await _scanner.scan(cidr=req.cidr, timeout=req.timeout)
        return {"ok": True, "found": results, "count": len(results)}
    except Exception as e:
        logger.exception("Erreur scan réseau")
        raise HTTPException(status_code=500, detail=f"Scan échoué: {e}")


@setup_router.post("/test_connection")
async def setup_test_connection(req: TestConnectionRequest, request: Request):
    _check_setup_allowed(request)
    if _scanner is None:
        raise HTTPException(status_code=503, detail="Scanner réseau non disponible")
    try:
        result = await _scanner.test_module(
            module_type=req.module_type,
            host=req.host,
            port=req.port,
            extra=req.extra,
        )
        return result
    except Exception as e:
        logger.exception("Erreur test connexion")
        return {"ok": False, "error": str(e)}


@setup_router.post("/complete")
async def setup_complete(req: CompleteWizardRequest, request: Request):
    """
    Finalise le wizard :
    1. Crée le compte admin (si bootstrap) OU vérifie que l'appelant est admin
    2. Applique la config sur _cfg
    3. Bascule first_boot=false
    4. Le restart de polling se fera via les callbacks de _cfg.update()
    """
    if not _db or not _cfg:
        raise HTTPException(status_code=503, detail="Backend pas prêt")

    auth = get_auth()
    bootstrap = _is_first_boot()

    if bootstrap:
        # Premier passage : création de l'admin
        if auth.has_any_user():
            # Cas limite : un user existe déjà mais first_boot=true (DB recréée?)
            # On le tolère seulement si c'est déjà un admin nommé pareil
            existing = auth.get_user_by_username(req.admin_username)
            if not existing:
                raise HTTPException(
                    status_code=400,
                    detail="Des utilisateurs existent déjà. Connectez-vous en admin pour terminer le wizard."
                )
        else:
            try:
                auth.create_user(req.admin_username, req.admin_password, role=ROLE_ADMIN)
                logger.info("Wizard: admin '%s' créé", req.admin_username)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
    else:
        # Wizard rejoué : exige admin
        token = request.cookies.get("seh_session")
        user = auth.user_from_token(token) if token else None
        if not user or user.role != ROLE_ADMIN:
            raise HTTPException(status_code=403, detail="Admin requis pour relancer le wizard")

    # Appliquer la config
    if req.config:
        try:
            _cfg.update(req.config)
            logger.info("Wizard: config mise à jour (%d sections)", len(req.config))
        except Exception as e:
            logger.exception("Erreur application config")
            raise HTTPException(status_code=500, detail=f"Config invalide: {e}")

    # Marquer first_boot terminé
    if bootstrap:
        _db.mark_first_boot_complete()
        logger.info("Wizard: first_boot=false → onboarding terminé")

    return {
        "ok": True,
        "bootstrap": bootstrap,
        "admin_created": bootstrap,
        "modules_enabled": req.enabled_modules,
        "next": "/login" if bootstrap else "/",
    }


@setup_router.post("/reopen")
async def setup_reopen(_admin=Depends(get_current_admin)):
    """
    Permet à un admin de re-déclencher le wizard pour ajouter un nouveau module.
    Ne supprime PAS les users ni la config existante.
    """
    if not _db:
        raise HTTPException(status_code=503, detail="DB pas prête")
    _db.set_meta("first_boot", "true")
    logger.info("Wizard ré-ouvert par admin (first_boot=true)")
    return {"ok": True, "message": "Wizard ré-ouvert. Le wizard reste accessible aux admins."}
