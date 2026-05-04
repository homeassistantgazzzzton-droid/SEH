"""
Smart Energy Hub — Support / Formulaire de contact (Sprint 10)

Permet à l'utilisateur d'envoyer un message au support technique via SMTP.
Les credentials SMTP sont configurés dans le wizard ou les Réglages
(stockés dans config.json section 'support').

Endpoints :
  GET  /api/support/config      → état SMTP (sans révéler le mot de passe)
  POST /api/support/test        (admin) → envoie un mail de test
  POST /api/support/send        → envoie un message au support
                                  (toujours public — sécurité anti-lockout)
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import socket
import ssl
from email.message import EmailMessage
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from auth import get_current_admin

logger = logging.getLogger(__name__)

support_router = APIRouter(prefix="/api/support", tags=["support"])

# Référence config injectée
_cfg = None


def init_support(cfg):
    global _cfg
    _cfg = cfg


# ═══════════════════════════════════════════════════════════════════════════
#  Modèles
# ═══════════════════════════════════════════════════════════════════════════

class SupportMessage(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    email: EmailStr
    subject: str = Field(..., min_length=1, max_length=256)
    body: str = Field(..., min_length=10, max_length=10000)
    include_diagnostics: bool = Field(default=False)


class SupportConfigUpdate(BaseModel):
    smtp_host: str
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_use_tls: bool = Field(default=True)
    smtp_username: str
    smtp_password: str
    from_email: EmailStr
    to_email: EmailStr  # destinataire = toi (l'éditeur du logiciel)


# ═══════════════════════════════════════════════════════════════════════════
#  SMTP send (helper sync exécuté dans un thread pour pas bloquer)
# ═══════════════════════════════════════════════════════════════════════════

def _send_email_sync(smtp_host: str, smtp_port: int, use_tls: bool,
                     username: str, password: str,
                     from_email: str, to_email: str,
                     subject: str, body: str,
                     reply_to: Optional[str] = None,
                     timeout: int = 15) -> dict:
    """Bloquant — à exécuter via asyncio.to_thread."""
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)

    try:
        if use_tls and smtp_port == 465:
            # SSL direct (port 465)
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=timeout) as srv:
                srv.login(username, password)
                srv.send_message(msg)
        else:
            # STARTTLS (port 587 typique) ou plain
            with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as srv:
                if use_tls:
                    srv.starttls(context=ssl.create_default_context())
                srv.login(username, password)
                srv.send_message(msg)
        return {"ok": True}
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "error": "auth_failed", "detail": str(e)}
    except (smtplib.SMTPException, socket.gaierror, OSError) as e:
        return {"ok": False, "error": "smtp_error", "detail": str(e)}


def _get_support_config() -> dict:
    if not _cfg:
        return {}
    return _cfg.get().get("support", {})


def _gather_diagnostics() -> str:
    """Collecte des infos système basiques pour aider au debug."""
    import platform
    import time
    lines = ["── Diagnostics Smart Energy Hub ──"]
    try:
        lines.append(f"OS: {platform.platform()}")
        lines.append(f"Python: {platform.python_version()}")
        lines.append(f"Hostname: {socket.gethostname()}")
        lines.append(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    except Exception as e:
        lines.append(f"(erreur diag: {e})")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@support_router.get("/config")
async def support_config_get():
    """Retourne l'état du module support (mot de passe masqué)."""
    cfg = _get_support_config()
    return {
        "configured": bool(cfg.get("smtp_host") and cfg.get("smtp_password")),
        "smtp_host": cfg.get("smtp_host", ""),
        "smtp_port": cfg.get("smtp_port", 587),
        "smtp_use_tls": cfg.get("smtp_use_tls", True),
        "smtp_username": cfg.get("smtp_username", ""),
        "from_email": cfg.get("from_email", ""),
        "to_email": cfg.get("to_email", ""),
        # password jamais retourné
    }


@support_router.post("/config")
async def support_config_set(req: SupportConfigUpdate, _admin=Depends(get_current_admin)):
    """Enregistre la config SMTP."""
    if not _cfg:
        raise HTTPException(status_code=503, detail="Config pas prête")
    _cfg.update({
        "support": {
            "smtp_host": req.smtp_host,
            "smtp_port": req.smtp_port,
            "smtp_use_tls": req.smtp_use_tls,
            "smtp_username": req.smtp_username,
            "smtp_password": req.smtp_password,
            "from_email": req.from_email,
            "to_email": req.to_email,
        }
    })
    return {"ok": True}


@support_router.post("/test")
async def support_test(_admin=Depends(get_current_admin)):
    """Envoie un mail de test à l'adresse configurée."""
    cfg = _get_support_config()
    if not cfg.get("smtp_host"):
        raise HTTPException(status_code=400, detail="SMTP non configuré")
    res = await asyncio.to_thread(
        _send_email_sync,
        cfg.get("smtp_host", ""),
        int(cfg.get("smtp_port", 587)),
        bool(cfg.get("smtp_use_tls", True)),
        cfg.get("smtp_username", ""),
        cfg.get("smtp_password", ""),
        cfg.get("from_email", ""),
        cfg.get("to_email", ""),
        "Simply Energy Home — Test SMTP",
        "Ce message confirme que la configuration SMTP fonctionne.\n\n— Simply Energy Home",
    )
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res)
    return res


@support_router.post("/send")
async def support_send(req: SupportMessage):
    """
    Envoi d'un message au support. Public exprès (pas d'auth requise) car
    si l'utilisateur est lockout, il faut quand même qu'il puisse demander
    de l'aide.
    """
    cfg = _get_support_config()
    if not cfg.get("smtp_host") or not cfg.get("to_email"):
        raise HTTPException(
            status_code=503,
            detail="Le support email n'est pas configuré sur cette instance"
        )

    body = (
        f"De: {req.name} <{req.email}>\n"
        f"Sujet: {req.subject}\n\n"
        f"{req.body}\n"
    )
    if req.include_diagnostics:
        body += "\n\n" + _gather_diagnostics()

    res = await asyncio.to_thread(
        _send_email_sync,
        cfg.get("smtp_host"),
        int(cfg.get("smtp_port", 587)),
        bool(cfg.get("smtp_use_tls", True)),
        cfg.get("smtp_username", ""),
        cfg.get("smtp_password", ""),
        cfg.get("from_email", ""),
        cfg.get("to_email", ""),
        f"[Simply Energy Home] {req.subject}",
        body,
        reply_to=req.email,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res)
    return {"ok": True, "message": "Message envoyé"}
