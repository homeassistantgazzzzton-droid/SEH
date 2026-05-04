# Patch Sprint 7 — Bug critique doublons d'agrégation

## 🚨 Le bug

**Symptôme** : valeurs hallucinantes (multipliées par 2, 3 ou plus) sur l'historique 30j/12m, page Rentabilité, totaux mois/année — tout ce qui agrège depuis `samples_daily`.

**Cause racine** : Les tables `samples_5min` et `samples_hourly` n'avaient **pas de contrainte UNIQUE sur la colonne `ts`**. Et les fonctions `_aggregate_5min` / `_aggregate_hourly` faisaient des `INSERT` bruts sans `ON CONFLICT`. Donc à chaque appel pour le même timestamp (recompute manuel, restart rapide du serveur, race condition), une nouvelle ligne était créée. L'agrégation `_aggregate_daily` faisait `SUM(pv_kwh) FROM samples_hourly` → totaux multipliés par le nombre de doublons.

**Reproduction** : 1 row attendu × 3 appels d'agrégation = 3 rows = SUM ×3. Une journée à 36 kWh PV se retrouvait stockée à 108 kWh en daily.

**Conditions de déclenchement réelles chez toi** :
- Tu as appuyé plusieurs fois sur "♻️ Recomputer agrégats 24h" pendant qu'on mettait au point le fix import/export
- Le crash conteneur (le bug `influx_publisher` manquant du Dockerfile) a redémarré le polling, qui a re-tenté l'agrégation horaire
- Cumul des deux → tes valeurs gonflées

## ✅ Fixes appliqués

### Backend `database.py`

1. **`UNIQUE INDEX` sur `samples_5min(ts)` et `samples_hourly(ts)`** (créés dans `_migrate` après dédoublonnage pour éviter de casser les vieilles DB)
2. **`INSERT … ON CONFLICT(ts) DO UPDATE`** dans `_aggregate_5min` et `_aggregate_hourly` : si on rejoue l'agrégation, on écrase au lieu de dupliquer
3. **Migration v3 automatique au démarrage** :
   - Détecte les `ts` dupliqués sur les deux tables
   - Garde la première ligne (`MIN(rowid)`) pour chaque ts
   - Crée le `UNIQUE INDEX`
   - Recompute toutes les `samples_daily` depuis le `samples_hourly` nettoyé
   - Marqueur d'idempotence (table `_seh_v3_daily_recomputed`) pour ne pas refaire au prochain démarrage
4. **Méthode `dedupe_aggregates(dry_run)`** publique pour relance manuelle

### Backend `main.py`

- `POST /api/maintenance/dedupe?dry_run={true|false}` : endpoint de dédoublonnage manuel

### Frontend `index.html`

- 2 nouveaux boutons dans **Réglages → 🔧 Maintenance** :
  - 👁️ **Détecter doublons** (dry_run)
  - 🧹 **Dédoublonner agrégats** (apply + recompute daily impactés)
- Les vues impactées (Rentabilité, Today vs Yesterday, Historique) se rafraîchissent automatiquement après dédoublonnage

### Tests `tests.py`

8 nouveaux tests (52 au total) :
- `TestUniqueIndexAggregates` (3) : UNIQUE INDEX présent sur DB neuve, idempotence des fonctions d'agrégation 5min et hourly
- `TestMigrationV3` (3) : dédoublonnage sur legacy DB, recompute daily après, idempotence migration
- `TestDedupeAggregates` (2) : dry_run inoffensif, comportement sans doublons

## 🎯 Comment ça se passe chez toi

**Au premier démarrage de la nouvelle version**, tu verras dans les logs :
```
Migration v3: samples_hourly contient X ts dupliqués. Dédoublonnage automatique en cours...
Migration v3: samples_hourly dédoublonné
Migration v3: UNIQUE INDEX créé sur samples_hourly.ts
Migration v3: recompute des samples_daily depuis samples_hourly nettoyés...
Migration v3: N samples_daily recomputés
```

**Aucune action manuelle nécessaire**. Tes graphiques 30j/12m, Rentabilité, totaux mois/année afficheront les bonnes valeurs après ce restart.

Si jamais tu vois encore des valeurs bizarres après, va dans **⚙️ Réglages → 🔧 Maintenance** et clique :
1. 👁️ **Détecter doublons** pour vérifier
2. Si la détection trouve quelque chose : 🧹 **Dédoublonner agrégats**

## 📝 Fichiers modifiés

| Fichier | Changements |
|---------|-------------|
| `backend/database.py` | UNIQUE INDEX, ON CONFLICT DO UPDATE sur 5min/hourly, migration v3 + `dedupe_aggregates()` + `_recompute_all_daily_from_hourly()` |
| `backend/main.py` | +endpoint `POST /api/maintenance/dedupe` |
| `backend/tests.py` | +8 tests pour UNIQUE, idempotence, migration v3, dedupe |
| `frontend/index.html` | +2 boutons UI dans section Maintenance, +handler JS `dbDedupe()` |

## ⚠️ Note sur les valeurs corrigées

Le dédoublonnage **garde la première ligne** pour chaque `ts`. C'est-à-dire la valeur "originelle" (avant les recompute). Si la valeur originale était fausse (par ex. samples manquants au moment du calcul), elle reste fausse — mais au moins elle n'est plus multipliée.

Pour ton cas, vu que les agrégats 5min/hourly originaux étaient corrects (calculés normalement par la boucle), le résultat post-dédoublonnage devrait être nickel.

## Tests : 52/52 ✅

Toujours zéro régression. Le filet de tests pytest qu'on a mis en place au sprint 5 a justement permis d'attraper ce bug pendant le développement du patch.
