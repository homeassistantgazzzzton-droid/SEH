# Sprint 15 — Diagnostics, Healthcheck Dashboard & Support Export

## 🎯 Objectif

Permettre à l'utilisateur lambda de :
1. **Voir** ce que fait son installation en temps réel (logs)
2. **Constater en un coup d'œil** si tout va bien (dashboard de santé)
3. **Aider le support efficacement** (export ZIP avec résumé textuel auto)

Sans jamais lui donner accès au code, au shell, ou à des informations sensibles.

## 🛡️ Sécurité — choix structurants

Tu as exprimé l'inquiétude que le client ne doit pas pouvoir entrer dans le programme. J'ai conçu le sprint avec ce garde-fou en tête :

| Risque | Mesure |
|--------|--------|
| Injection shell via nom de source | **Whitelist en dur** dans `log_reader.py` (4 sources). Tout ce qui n'est pas dans la whitelist est refusé avec un message générique |
| Lecture de fichiers arbitraires | Le module n'utilise **jamais** `open()` sur un chemin user-controllable. Seuls `docker logs <container_fixe>` et `journalctl -u <service_fixe>` sont appelés |
| `shell=True` exploitable | **Jamais utilisé**. Tous les `subprocess.run` reçoivent une liste d'args, pas une chaîne |
| Tokens / mots de passe dans les logs | Filtrage automatique des patterns sensibles (Bearer, password=, JWT, smtp_password, etc.) → `[REDACTED]` |
| Caractères de contrôle ANSI dans la sortie | Strip systématique avant envoi au frontend |
| Dépassement mémoire (logs énormes) | Cap en dur : 500 lignes max (UI) ou 2000 max (export) |
| Permissions | Tous les endpoints `/api/logs/*` et `/api/support/*` réservés aux **admins** |

## 📦 Livrables

### Backend

| Fichier | Action | Rôle |
|---------|--------|------|
| `backend/log_reader.py` | **Nouveau** (~190 lignes) | Lecture sécurisée Docker + journalctl, whitelist, sanitization |
| `backend/health_tracker.py` | **Nouveau** (~210 lignes) | Tracker d'events 24h avec persistance simple, score 1h, buckets horaires |
| `backend/support_export.py` | **Nouveau** (~180 lignes) | Génère ZIP avec SUMMARY.txt auto, diag, logs, health |
| `backend/main.py` | **Modifié** | Endpoints `/api/logs/*`, `/api/health/dashboard`, `/api/support/export` ; init du health tracker au lifespan ; record d'events depuis les boucles polling |

### Frontend

| Fichier | Action | Détail |
|---------|--------|--------|
| `frontend/index.html` | **Modifié** | 3 nouveaux panneaux dans Réglages : État de santé (avec graphe 24h), Logs (4 onglets), Contact support (bouton télécharger) |

### Tests

| Fichier | Couverture |
|---------|------------|
| `backend/test_sprint15_e2e.py` | 32 assertions : whitelist, injection refusée, permissions, structure ZIP, sanitization, contenu SUMMARY |

## ✅ Validation

Tous les tests passent (sprints 10, 13, 14, 15 + non-régression) :

```bash
cd backend
python3 test_sprint10_e2e.py   # OK — auth + wizard
python3 test_sprint13_e2e.py   # OK — perf monitor + lazy load
python3 test_sprint14_e2e.py   # OK — OTA updates
python3 test_sprint15_e2e.py   # OK — logs + health + support
```

Highlights des tests sprint 15 :
- ✓ Whitelist : seules les 4 sources autorisées (app, network, update, watchdog)
- ✓ Injection shell `app;rm -rf /` refusée proprement
- ✓ Source inconnue → 200 avec `available: false` (pas de 500, pas de leak)
- ✓ User lambda refusé sur logs et export (admin only)
- ✓ Health dashboard accessible aux users lambda (read-only)
- ✓ ZIP support correctement structuré (SUMMARY, diag, logs, health, META)
- ✓ SUMMARY contient les infos clés
- ✓ Pas de credentials dans le diagnostics

## 🎨 Ce que voit l'utilisateur

Onglet **Réglages**, dans l'ordre :

1. **Performances système** (sprint 13) — CPU, RAM, latence
2. **Mise à jour du logiciel** (sprint 14) — version, channel, bouton MAJ
3. **État de santé** (sprint 15) — score 0-100, mini-graphe 24h, échecs récents
4. **Logs** (sprint 15) — onglets app/réseau/update/watchdog, 200 dernières lignes avec scroll
5. **Contact support** (sprint 15) — un seul bouton "Télécharger le diagnostic (.zip)"

L'utilisateur n'a aucun moyen de :
- Lancer une commande
- Voir le code source
- Accéder à un shell
- Lire un fichier arbitraire
- Voir les mots de passe / tokens

## 📋 Ce qu'il y a dans le ZIP support

```
seh-support-2024-01-15_14-30-00.zip
├── SUMMARY.txt              ← Lu en premier par toi (résumé textuel auto)
├── diagnostics.json         ← Sortie complète /api/diagnostics (sanitisée)
├── META.json                ← Date d'export, schema version
├── logs/
│   ├── app.txt              ← 1000 dernières lignes du conteneur
│   ├── network.txt          ← journalctl seh-network-setup
│   ├── update.txt           ← journalctl seh-update
│   └── watchdog.txt         ← journalctl seh-watchdog
└── health/
    ├── snapshot.json        ← Score 1h + counts par type
    ├── events.json          ← Tous les events des 24h
    └── recent_failures.json ← Top 30 derniers échecs
```

Exemple de SUMMARY.txt (extrait réel des tests) :

```
═════════════════════════════════════════════════════════════
  Simply Energy Home — Diagnostic Support
═════════════════════════════════════════════════════════════
Généré : 2024-01-15 14:30:00 +0100

── Version ──
  Version installée : v1.0.5
  Channel           : stable
  Image             : ghcr.io/.../seh:1.0.5
  Installée le      : 2024-01-12 09:14:23

── Système ──
  Plateforme        : aarch64
  Pi détecté        : oui
  Resources limitées: oui  (Pi Zero 2W)
  RAM totale        : 512 Mo

── Performance courante ──
  CPU global        : 25.4%
  RAM utilisée      : 68.5%
  RSS app           : 78.3 Mo
  Température       : 52.1°C

── Modules activés ──
  • inverter:voltronic
  • bms:pylontech
  • finance
  • mqtt

── Santé applicative ──
  Score 1h          : 95/100
  Échecs 1h         : 1
  Events 24h        : 230

── Échecs récents (top 5) ──
  [14:25:33] inverter_fail: Voltronic timeout sur QID après 5 tentatives
```

En 30 secondes de lecture tu sais : Pi Zero 2W, version v1.0.5 stable, modules actifs, score correct, 1 fail récent isolé. Plus besoin de demander 5 questions au client.

## 🩺 Comment fonctionne le health tracker

Le tracker enregistre des **events** (typés) à chaque cycle de polling :

| Event | Quand | Catégorie |
|-------|-------|-----------|
| `inverter_ok` | Polling onduleur réussi | OK |
| `inverter_fail` | Exception dans la boucle inverter | FAIL |
| `bms_ok` / `bms_fail` | Idem pour BMS | OK / FAIL |
| `app_start` | Démarrage du backend | OK |
| `module_error` | Erreur générique d'un module | FAIL |

Le **score 1h** = `100 × oks / (oks + fails)` calculé sur la dernière heure.

Le **graphe 24h** = 24 buckets horaires, chacun colorié selon son score :
- 🟢 ≥ 90 (vert)
- 🟡 70-89 (orange)
- 🔴 < 70 (rouge)

L'utilisateur voit en un coup d'œil si l'installation a été stable la nuit dernière, ou si elle a eu un creux à 14h.

### Persistance

Les events sont sauvés dans `/data/health_events.json` toutes les 30s (throttle).
Au redémarrage, le tracker recharge les events des 24h précédentes — pas de perte d'historique sur les reboots de routine.

## 🔮 Prochain sprint (16)

Idées :
- **Notifications email automatiques** quand le score 1h chute < 70
- **Dashboard public read-only** (URL accessible sans login pour partager avec un proche)
- **Statistiques mensuelles** de stabilité (uptime %, MTBF)
- **Support multi-langue** (anglais/français/allemand pour vendre à l'international)

## 🔐 Note sur la confidentialité

Le ZIP de support contient :
- ✅ Logs applicatifs (potentiellement avec emails/URLs si l'app les log)
- ✅ Configuration (sans mots de passe)
- ✅ Métriques système (CPU, RAM, modèle de Pi, etc.)

Le ZIP **ne contient pas** :
- ❌ Mots de passe (déjà filtrés par diagnostics + log_reader)
- ❌ Tokens API ou JWT (idem)
- ❌ Données utilisateur stockées en DB (consommation, batteries, etc. — ce sont des infos techniques à part)

L'utilisateur **doit consentir** explicitement au téléchargement (clic sur le bouton). Le serveur ne génère jamais le ZIP en arrière-plan.
