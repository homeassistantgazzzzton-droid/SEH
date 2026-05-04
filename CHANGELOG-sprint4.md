# Sprint 4 — Parc batteries + ROI/Rentabilité

## ✅ Livré

### 1. 🗂️ Vue Parc batteries (`/api/fleet` + onglet "Parc")

Grille agrégée multi-groupes (Pylontech + JK-BMS) avec :
- **Résumé du parc** : SoC moyen, puissance totale, énergie stockée / capacité, online/offline, alarmes, états
- **Carte par batterie** : jauge SoC circulaire animée (couleur selon niveau), badge état, métriques complètes, alarmes
- **Filtres** : Tous / par groupe
- **Tris** : défaut / SoC ↑↓ / alarmes en tête / cycles décroissants
- Batteries offline grisées, celles en alarme bordurées rouge

### 2. 💰 Suivi de rentabilité / ROI (`/api/roi` + onglet "Rentabilité")

- **4 KPIs** : coût installation, économies totales, économies mensuelles, break-even
- **Barre de progression** du remboursement
- **Graphiques** : historique cumulatif + projection 20 ans (inflation EDF + dégradation panneaux)
- **3 cartes détails** : scénario sans solaire vs réalité avec solaire vs hypothèses
- **Réglages dédiés** : coût installation, date mise en service, inflation %, dégradation %

### 3. 🐛 Correction critique : calcul import/export des agrégats

**Symptôme** : export_kwh = 0 sur la page Rentabilité (et aussi sur les graphiques 30j/12m) même pour des installations qui exportent quotidiennement.

**Cause** : `_aggregate_hourly` calculait `import_kwh = max(0, AVG(grid))` et `export_kwh = abs(min(0, AVG(grid)))`. Si dans la même heure l'install alterne +1500W (import nuit) et -500W (export jour), la moyenne horaire tombe à +500W → 500 Wh d'import comptabilisés, 0 d'export, alors qu'en réalité il y a eu 750 Wh ET 250 Wh.

**Correctif** :
- Ajout colonnes kWh dans `samples_5min` + migration DB automatique (ALTER TABLE idempotent)
- `_aggregate_5min` calcule désormais avec `CASE WHEN grid_power > 0 / < 0` séparément sur les samples raw à 10s → capture les alternances
- `_aggregate_hourly` SOMME depuis samples_5min au lieu de recalculer faux

**Test validé** : scénario 30 min à +1500W puis 30 min à -500W → attendu 0.75 kWh import + 0.25 kWh export ; obtenu 0.746 + 0.252 ✓ (avant fix : 0.5 + 0.0 ❌)

### 4. 🔄 Option d'inversion de signe Victron

Certaines installations Victron (mauvais câblage CT, firmware Venus OS spécifique, compteur ET340 mal configuré) renvoient les signes inversés : `grid_power > 0` quand on exporte et `< 0` quand on importe. Dans ce cas :
- Le dashboard affichait les flèches dans le mauvais sens ("Grid → Load" alors qu'on exporte)
- L'import/export était comptabilisé à l'envers → export réel rangé dans import_kwh

**Solution** : deux checkboxes dans les réglages Victron :
- **Inverser signe Grid** (si import/export inversés)
- **Inverser signe Batterie** (si charge/décharge inversés)

L'inversion est appliquée dans la boucle de polling Victron au niveau du cache, donc dashboard + DB + MQTT voient tous la version corrigée, d'un coup.

### 5. 🔧 Maintenance DB (section Réglages)

Trois boutons :
- **♻️ Recomputer agrégats 24h** : reconstruit les agrégats 5min et hourly des dernières 24h à partir des samples_raw (seule fenêtre rattrapable exactement)
- **👁️ Prévisualiser backfill export** : affiche ce qui serait corrigé pour les jours passés
- **💾 Appliquer backfill export** : estime `export = max(0, PV − Load + Import + Décharge − Charge)` pour les jours où export_kwh=0 mais PV>Load. Non destructif.

## 📝 Fichiers modifiés

| Fichier | Changements |
|---------|-------------|
| `backend/main.py` | +endpoints `/api/fleet`, `/api/roi`, `/api/maintenance/recompute`, `/api/maintenance/backfill_export` ; inversion de signe appliquée dans `victron_loop` |
| `backend/database.py` | +colonnes kWh dans samples_5min, migration idempotente, calcul import/export corrigé, méthodes `recompute_recent()` et `backfill_historical_export()` |
| `backend/config_manager.py` | +section `roi`, +options `invert_grid_sign` et `invert_battery_sign` dans victron |
| `frontend/index.html` | +2 onglets (Parc, Rentabilité), +pages associées, +CSS, +JS (`loadFleet`, `renderFleet`, `loadROI`, `renderROI`, `renderROICharts`, `dbRecompute`, `dbBackfillExport`), +checkboxes inversion signe dans section Victron, +sections ROI et Maintenance |

## 🚫 Non livré (reporté au sprint 5)

- **Contrôle onduleur** (priorités source/charge)

## 🧪 Tests effectués

- Compilation Python complète ✅
- Validation JS (`node --check`) ✅
- Endpoint `/api/fleet` testé : mix Pylontech + JK-BMS, online + offline + alarme, stats correctes ✅
- Endpoint `/api/roi` testé : 180 jours simulés, break-even/projection/ROI annualisé cohérents ✅
- Test du bug d'agrégation import/export : 0.75 / 0.25 kWh attendus, obtenus 0.746 / 0.252 ✅
- Migration DB v1→v2 testée, données préservées ✅
- Backfill export testé, idempotent ✅
- Inversion de signe Grid/Battery testée end-to-end ✅

## 🎯 Procédure pour Alexis (ton cas spécifique)

1. Installer la nouvelle version (la migration DB s'applique automatiquement)
2. Aller dans **⚙️ Réglages** → section "⚡ Onduleur" (type Victron)
3. **Cocher "Inverser signe Grid"** (et éventuellement "Inverser signe Batterie" si tu vois aussi charge/décharge inversés)
4. **💾 Sauvegarder** — les prochaines minutes, le dashboard doit afficher les flèches correctement et `grid_power` avec le bon signe
5. Attendre au moins 5-10 minutes pour que de nouveaux samples 5min se créent correctement
6. Aller dans **⚙️ Réglages** → section "🔧 Maintenance base de données"
7. Cliquer **♻️ Recomputer agrégats 24h** (répare pile-poil les dernières 24h depuis samples_raw — attention, les raw datent d'avant l'inversion, donc ils seront inversés au recomputing **uniquement** si le polling continu a produit de nouveaux raw avec la bonne convention depuis le check)
8. Cliquer **👁️ Prévisualiser backfill export** pour voir les jours passés qui seraient corrigés par estimation
9. Si OK, **💾 Appliquer backfill export**
10. Retourner sur **📈 Rentabilité** pour voir le résultat

### ⚠️ Note importante sur le recompute

Le recompute agrégats utilise les **samples_raw** (bruts à 10s, rétention 24h). Ces raw ont été enregistrés avec la convention de signe **telle que lue à l'époque**. Donc :
- Si tu actives l'inversion **avant** de faire le recompute, les 24h anciennes ont été enregistrées avec le mauvais signe et le recompute va juste recréer les kWh avec la nouvelle formule (correcte structurellement) mais basée sur des signes inversés → resultat toujours faux
- Solution la plus simple : activer l'inversion, attendre 24h complètes pour que tous les raw aient été remplacés avec le bon signe, puis cliquer recompute

Pour l'historique passé (daily > 24h), le backfill par équation de conservation (PV - Load + Import + Discharge - Charge) est indépendant du signe puisqu'il se base sur des valeurs agrégées cumulatives — ça marche quel que soit le bug initial.
