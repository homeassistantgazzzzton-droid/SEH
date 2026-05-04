# Patch Sprints 10-15 — Simply Energy Home

Ce patch contient TOUS les fichiers nouveaux ou modifiés entre les sprints 10 et 15.

## Comment appliquer

1. Sauvegarde ton projet existant (au cas où) :
   ```bash
   cp -r mon-projet mon-projet-backup
   ```

2. Copie tous les fichiers de ce patch DANS ton projet existant, en préservant 
   l'arborescence. Les fichiers existants seront ÉCRASÉS (c'est voulu).

3. Vérifie que ces fichiers de ton projet pré-sprint 10 SONT TOUJOURS PRÉSENTS
   (ils sont nécessaires pour que main.py fonctionne) :
   - backend/voltronic.py
   - backend/victron.py
   - backend/pylontech.py
   - backend/jkbms_modbus.py
   - backend/energy_finance.py
   - backend/alerts.py
   - backend/mqtt_publisher.py
   - backend/solar_forecast.py
   - backend/influx_publisher.py
   - backend/solax_fleet.py
   - backend/inverters/__init__.py
   - backend/inverters/base.py
   - backend/inverters/modbus_client.py
   - backend/inverters/plugin_solax_x1.py
   - backend/tests.py (ancien tests, optionnel)
   - frontend/sw.js
   - docker-compose.yml

4. AVANT DE PUSH : remplace homeassistantgazzzzton-droid par ton handle dans tous les fichiers :
   ```bash
   # Linux/Mac/GitBash
   grep -rl "homeassistantgazzzzton-droid" . | xargs sed -i 's|homeassistantgazzzzton-droid|<TON_HANDLE>|g'
   ```

5. Test en local AVANT le push GitHub :
   ```bash
   cd backend
   pip install -r requirements.txt
   pip install pytest httpx
   python3 test_sprint10_e2e.py
   python3 test_sprint13_e2e.py
   python3 test_sprint14_e2e.py
   python3 test_sprint15_e2e.py
   ```
   Tous doivent finir par "🎉 Tous les tests ... PASS"

## Que contient ce patch

### Backend (sprint 10+13+14+15)
- main.py (CRUCIAL — réécrit avec auth, perf, update, logs, health)
- auth.py (NOUVEAU — JWT auth)
- setup.py (NOUVEAU — wizard d'onboarding)
- network_scanner.py (NOUVEAU — détection auto modules)
- support.py (NOUVEAU — formulaire contact SMTP)
- perf_monitor.py (NOUVEAU — CPU/RAM watchdog)
- updater.py (NOUVEAU — OTA updates)
- log_reader.py (NOUVEAU — logs viewer sécurisé)
- health_tracker.py (NOUVEAU — santé 24h)
- support_export.py (NOUVEAU — ZIP support)
- database.py (MODIFIÉ — migration v5)
- config_manager.py (MODIFIÉ — section support)
- requirements.txt (MODIFIÉ — bcrypt, PyJWT, email-validator)
- 4 fichiers test_sprint*_e2e.py

### Frontend (sprint 10+13+14+15)
- index.html (RÉÉCRIT — bootstrap auth + 5 panneaux dans Réglages)
- login.html (NOUVEAU — page connexion)
- setup.html (NOUVEAU — wizard 6 étapes)
- manifest.json (MODIFIÉ — branding Simply Energy Home)

### Services hôte (sprint 11+14)
- seh-network-setup.{py,service} — hotspot wifi 1er boot
- seh-update.{sh,service,timer} — orchestrateur MAJ
- seh-watchdog.{py,service} — surveillance post-MAJ
- captive_portal.html — page config wifi mobile
- install.sh — installeur tout-en-un (MODIFIÉ pour sprint 14)

### Scripts (sprint 12+13)
- install-seh.sh — installeur universel curl|bash
- test-install-locally.sh — test dans Docker
- bench-pi.sh — benchmark sur Pi

### CI/CD GitHub Actions (sprint 12)
- .github/workflows/tests.yml
- .github/workflows/build-docker.yml
- .github/workflows/build-rpi-image.yml
- .github/workflows/build-x86-image.yml

### Build d'image RPi (sprint 12)
- pi-gen-stage/ — stage greffé sur pi-gen
- Dockerfile (NOUVEAU)

