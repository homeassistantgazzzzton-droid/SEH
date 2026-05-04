# Sprint 9 — Onglet Solax dédié avec Flow Diagram

## ✅ Livré

### 🔆 Nouvel onglet Solax dans la nav

Visible automatiquement dès qu'au moins un onduleur Solax remonte des données. Tab caché si pas de Solax configuré.

### Flow diagram horizontal animé

Représentation graphique en temps réel des flux énergétiques façon "synoptique" :

```
PV1 ──┐                    ┌──→ Maison
      ├──→ [Onduleur] ─────┤
PV2 ──┘                    └──→ Réseau
                │
                ↓
           Batterie
```

**Caractéristiques** :
- 6 nœuds visuels avec icônes : ☀️ PV1, ☀️ PV2, ⚡ Onduleur, 🏠 Maison, ⚡ Réseau, 🔋 Batterie
- Flèches **animées** quand le flux est actif (`stroke-dasharray` + `animation: flow`)
- Codage couleur :
  - 🟡 Jaune : flux PV
  - 🟢 Vert : maison
  - 🔵 Bleu : export réseau
  - 🔴 Rouge : import réseau (couleur dynamique selon sens)
  - 🟣 Violet : batterie
- Pulse autour des nœuds actifs (production/conso > 10W)
- Barre de SoC sous le node Batterie (0-100%)
- Labels de valeurs en W/kW à côté de chaque flèche
- Switch automatique import ↔ export du réseau avec changement de couleur

### Graphique 24h temps réel

Chart.js avec 4 lignes superposées : PV (aire jaune), Maison (vert), Réseau (bleu pointillé), Batterie (violet).
Buffer mémoire JS (max 24h × 6/min = 8640 points) qui s'alimente à chaque tick de polling.
Pas de stockage DB pour ce sprint (les données restent en RAM tant que la page est ouverte).

### Stats cumulées (4 widgets)

- ☀️ Production jour (yield_today + total cumulé)
- 🏠 Charge jour (estimation balance énergétique)
- 📤 Vente jour (export_today + total)
- 📥 Achat jour (import_today + total)

### Header dynamique

- Type d'onduleur, serial, firmware
- Indicateur en ligne/hors ligne avec point coloré
- Status courant (Normal, Standby, Fault...)
- Délai depuis la dernière MAJ

### Auto-refresh intelligent

- Refresh toutes les 10s tant que l'onglet Solax est actif
- Pause automatique quand l'utilisateur change d'onglet
- Buffer 24h glissant (filtre les samples > 24h)

## 📝 Fichiers modifiés

| Fichier | Changements |
|---------|-------------|
| `frontend/index.html` | +Tab Solax + page + ~250 lignes de CSS/JS pour flow diagram, chart 24h, stats |
| `backend/main.py` | +`solax` dans `/api/status` et WebSocket `full_update` |

## 🎨 Détails graphiques

- Layout horizontal responsive (passe en stack vertical sur < 900px)
- Gradient subtil sur le wrapper (jaune en haut-droite, vert en bas-gauche)
- Cartes nœud avec ombrage 3D et bordure colorée selon état
- Animation `pulse` douce sur les nœuds actifs (2s linear infinite)
- Police monospace pour les valeurs numériques (alignement parfait)
- Glassmorphism léger sur les labels de flèches
- Mode sombre cohérent avec le reste de l'app

## 🚀 Pour tester chez toi

```bash
cd /opt/smart-energy-hub
unzip -o /opt/smart-energy-hub.zip 2>/dev/null || true
docker compose up -d --build
```

Va sur l'app web, le tab **🔆 Solax** apparaît automatiquement à côté du tab Leaf (puisque ton Solax remonte déjà des données). Clique dessus, tu verras :
- Le flow diagram avec PV/Onduleur/Maison/Grid/Batterie animés selon le flux actuel
- Les valeurs en direct (puissances, tensions, courants)
- Le graphique 24h qui se construit progressivement (vide au début, remplis après quelques minutes de polling)
- Les stats cumulées du jour

## 🎯 Prochaines étapes possibles

1. **Persister les données Solax en DB** pour avoir un graphique 24h dès l'ouverture (sans attendre que le buffer se remplisse)
2. **Activer l'écriture sécurisée** (sliders limite charge/décharge batterie)
3. **Ajouter Growatt + Solis** pour ouvrir aux autres utilisateurs
4. **Mode RTU/USB** pour les utilisateurs sans passerelle Ethernet
