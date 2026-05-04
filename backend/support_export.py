"""
Smart Energy Hub — Support Export (Sprint 15)

Génère un ZIP contenant :
  - SUMMARY.txt          : résumé textuel auto-généré (top du ZIP, lu en premier)
  - diagnostics.json     : sortie de /api/diagnostics
  - logs/<source>.txt    : 1 fichier par source de logs (whitelistée)
  - health_events.json   : events des dernières 24h
  - perf_history.json    : courbe perf 30 min

Le ZIP est généré en mémoire (BytesIO) — pas de fichier temporaire sur disque,
donc pas de risque de fuite ni de remplissage du disque.

Fonction d'entrée :
    build_support_zip(diagnostics, perf_history, health_events) → bytes

Le résumé textuel essaie d'identifier les patterns les plus utiles pour le support :
  - Version installée + date
  - Plateforme (Pi/x86) + RAM totale
  - Modules activés
  - Score de santé 1h et 24h
  - Top 5 erreurs récentes
  - Dernier état de MAJ
  - Uptime
"""
from __future__ import annotations

import io
import json
import logging
import time
import zipfile
from typing import Optional

logger = logging.getLogger(__name__)


def _ts_human(ts: float) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (ValueError, OverflowError):
        return "—"


def _format_uptime(seconds: int) -> str:
    if not seconds or seconds < 0:
        return "—"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days}j {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def build_summary(diag: dict, health_snapshot: dict,
                  recent_failures: list[dict]) -> str:
    """Génère le SUMMARY.txt — premier fichier que le support lit."""
    lines = []
    lines.append("═════════════════════════════════════════════════════════════")
    lines.append("  Simply Energy Home — Diagnostic Support")
    lines.append("═════════════════════════════════════════════════════════════")
    lines.append(f"Généré : {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    lines.append("")

    # Version
    v = diag.get("version", {})
    lines.append("── Version ──")
    lines.append(f"  Version installée : {v.get('version', '?')}")
    lines.append(f"  Channel           : {v.get('channel', '?')}")
    lines.append(f"  Image             : {v.get('image_ref', '?')}")
    lines.append(f"  Installée le      : {_ts_human(v.get('installed_at', 0))}")

    # Update status
    upd = diag.get("update_status", {})
    if upd.get("state") and upd["state"] != "idle":
        lines.append("")
        lines.append("── Dernière mise à jour ──")
        lines.append(f"  État              : {upd.get('state', '?')}")
        lines.append(f"  Cible             : {upd.get('target_version', '?')}")
        lines.append(f"  Précédente        : {upd.get('previous_version', '?')}")
        lines.append(f"  Demandée à        : {_ts_human(upd.get('requested_at', 0))}")
        lines.append(f"  Terminée à        : {_ts_human(upd.get('completed_at', 0))}")
        if upd.get("error"):
            lines.append(f"  ⚠ Erreur          : {upd['error']}")

    # Système
    perf_info = diag.get("perf", {}).get("info", {})
    lines.append("")
    lines.append("── Système ──")
    lines.append(f"  Plateforme        : {perf_info.get('platform', '?')}")
    lines.append(f"  Pi détecté        : {'oui' if perf_info.get('is_pi') else 'non'}")
    lines.append(f"  Resources limitées: {'oui' if perf_info.get('is_low_resource') else 'non'}")
    lines.append(f"  RAM totale        : {perf_info.get('mem_total_mb', '?')} Mo")
    lines.append(f"  CPU cœurs         : {perf_info.get('cpu_count', '?')}")

    # État courant perf
    cur = diag.get("perf", {}).get("current") or {}
    if cur:
        lines.append("")
        lines.append("── Performance courante ──")
        lines.append(f"  CPU global        : {cur.get('cpu_global_pct', '?')}%")
        lines.append(f"  CPU app           : {cur.get('cpu_proc_pct', '?')}%")
        lines.append(f"  RAM utilisée      : {cur.get('mem_used_pct', '?')}%")
        lines.append(f"  RAM dispo         : {cur.get('mem_available_mb', '?')} Mo")
        lines.append(f"  RSS app           : {cur.get('rss_mb', '?')} Mo")
        lines.append(f"  Latence poll      : {cur.get('poll_latency_ms', '?')} ms")
        if cur.get("cpu_temp_c"):
            lines.append(f"  Température       : {cur['cpu_temp_c']:.1f}°C")
        if cur.get("throttled"):
            lines.append(f"  ⚠ THROTTLE actif (CPU surchargé)")

    # Modules
    lines.append("")
    lines.append("── Modules activés ──")
    modules = diag.get("modules_enabled", [])
    if modules:
        for m in modules:
            lines.append(f"  • {m}")
    else:
        lines.append("  (aucun)")

    # Uptime
    lines.append("")
    lines.append(f"Uptime              : {_format_uptime(diag.get('uptime_s', 0))}")
    lines.append(f"Premier démarrage   : {'oui (wizard pas encore fait)' if diag.get('first_boot') else 'non'}")
    lines.append(f"Utilisateurs        : {diag.get('auth_users_count', 0)}")

    # Health
    lines.append("")
    lines.append("── Santé applicative ──")
    lines.append(f"  Score 1h          : {health_snapshot.get('score_1h', '?')}/100")
    lines.append(f"  Échecs 1h         : {health_snapshot.get('fails_1h', 0)}")
    lines.append(f"  OK 1h             : {health_snapshot.get('oks_1h', 0)}")
    lines.append(f"  Events 24h        : {health_snapshot.get('total_events_24h', 0)}")

    # Recent failures
    if recent_failures:
        lines.append("")
        lines.append("── Échecs récents (top 5) ──")
        for f in recent_failures[:5]:
            ts = _ts_human(f.get("ts", 0))
            t = f.get("type", "?")
            d = f.get("detail", "")[:80]
            lines.append(f"  [{ts}] {t}: {d}")

    lines.append("")
    lines.append("═════════════════════════════════════════════════════════════")
    lines.append(" Pour le support : joignez ce fichier ZIP en pièce jointe.")
    lines.append(" Aucun mot de passe ni token n'a été exporté.")
    lines.append("═════════════════════════════════════════════════════════════")

    return "\n".join(lines)


def build_support_zip(diagnostics: dict,
                      health_snapshot: dict,
                      health_events: list[dict],
                      recent_failures: list[dict],
                      logs_by_source: dict[str, list[str]]) -> bytes:
    """
    Construit le ZIP en mémoire et retourne ses bytes.

    Args:
        diagnostics      : dict de /api/diagnostics
        health_snapshot  : dict de health_tracker.snapshot()
        health_events    : list de health_tracker.all_events()
        recent_failures  : list de health_tracker.recent_failures()
        logs_by_source   : {source_id: list_of_lines}
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 1. SUMMARY au top — c'est le premier fichier que le support voit
        summary = build_summary(diagnostics, health_snapshot, recent_failures)
        zf.writestr("SUMMARY.txt", summary)

        # 2. Diagnostics complets (sanitisés par main.py — secrets déjà retirés)
        zf.writestr("diagnostics.json", json.dumps(diagnostics, indent=2, default=str))

        # 3. Health events 24h
        zf.writestr("health/events.json", json.dumps(health_events, indent=2))
        zf.writestr("health/snapshot.json", json.dumps(health_snapshot, indent=2))
        zf.writestr("health/recent_failures.json", json.dumps(recent_failures, indent=2))

        # 4. Logs par source
        for src, lines in logs_by_source.items():
            content = "\n".join(lines) if lines else "(aucun log disponible)"
            zf.writestr(f"logs/{src}.txt", content)

        # 5. Marqueur de version + timestamp pour traçabilité
        meta = {
            "exported_at": time.time(),
            "exported_at_human": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "exporter_version": "sprint15",
            "schema_version": 1,
        }
        zf.writestr("META.json", json.dumps(meta, indent=2))

    return buf.getvalue()
