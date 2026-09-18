# Déploiement

Comment le logiciel est installé sur cette machine, et comment livrer une
modification sans casser la version utilisée au quotidien.

---

## Principe : deux modes

| | **Version déployée** (usage quotidien) | **Mode développement** (modifier) |
|---|---|---|
| Lancement | icône du Bureau, `make up`, `make start` | `make dev` |
| Code | **figé dans l'image Docker**, au moment du déploiement | dossier `source/` du dépôt, monté en direct |
| Modifier un fichier | n'a **aucun effet** sur le logiciel en service | pris en compte aussitôt (l'API se recharge) |
| Version affichée (Réglages → À propos) | `2026.09.17-143313-54c4be6` | `dev (code du dépôt)` |

Les données ne dépendent pas du mode : vidéos (`MEDIA_PATH`), résultats
(`OUTPUT_PATH`), base des traitements, modèles et index de recherche sont
conservés d'une version à l'autre.

Une version s'appelle `AAAA.MM.JJ-HHMMSS-<commit>` : date du déploiement et
commit Git. Le suffixe `-modifie` signale des modifications non commitées au
moment du déploiement.

---

## Modifier le logiciel, étape par étape

```bash
# 1. Passer en mode développement : le code du dépôt est utilisé en direct
make dev

# 2. Modifier le code (source/, source/static/index.html…), puis vérifier dans
#    l'interface. L'API se recharge seule ; pour le worker :
docker compose restart worker

# 3. Lancer les tests (≈ 15 s, dans Docker)
make test

# 4. Enregistrer la modification
git add -A && git commit -m "Ce qui change et pourquoi"

# 5. Déployer : l'icône du Bureau ouvrira la nouvelle version
make deploy
```

`make deploy` enchaîne, et s'arrête à la première erreur **sans rien changer**
au logiciel en service :

1. **vérifie qu'aucun traitement n'est en cours** — redémarrer le worker les
   interromprait (ils reprendraient ensuite là où ils en étaient) ;
2. **lance les tests** ;
3. **construit les images** de la nouvelle version (quelques secondes : seul le
   code change, les dépendances sont en cache) ;
4. **bascule** l'API et le worker sur la nouvelle version ;
5. **vérifie** que l'interface répond avec le bon numéro de version — sinon,
   **retour automatique** à la version précédente ;
6. **supprime les images anciennes**, en gardant les 3 dernières versions.

Sans passer par le mode développement (petite modification sûre) : modifier,
`make test`, `make deploy`.

---

## En cas de problème après un déploiement

```bash
make rollback      # revient à la version précédente, en quelques secondes
make rollback      # à nouveau : revient à la version qui vient d'être quittée
make versions      # version en service, précédente, historique
```

Exemple :

```
En service  : 2026.09.17-143313-54c4be6
Précédente  : 2026.09.17-1431-54c4be6
Répond      : 2026.09.17-143313-54c4be6

Historique (derniers déploiements) :
  2026-09-17 14:32   2026.09.17-1431-54c4be6     Zip lisible sous Windows…
  2026-09-17 14:33   2026.09.17-143313-54c4be6   Nouvelle interface
```

---

## Options

| Commande | Effet |
|---|---|
| `FORCE=1 make deploy` | déploie même si des traitements sont en cours (ils reprendront) |
| `SKIP_TESTS=1 make deploy` | saute les tests — seulement s'ils viennent de passer |
| `GPU=0 make deploy` | images CPU (machine sans carte NVIDIA) ; détecté automatiquement sinon |
| `make test-all` | tests, puis vérification de bout en bout sur la version en service |

---

## Où sont les choses

| Élément | Emplacement |
|---|---|
| Version en service / précédente | `.deploy/current`, `.deploy/previous` (non versionnés) |
| Historique des déploiements | `.deploy/historique` |
| Images | `transcription-video-audio:gpu-<version>` (GPU) ou `:<version>` (CPU) — `docker image ls transcription-video-audio` |
| Script | `scripts/deployer.sh`, appelé par `make deploy`, `make rollback`, `make versions` |
| Mode développement | `docker-compose.dev.yml` (montage du code, rechargement de l'API) |

---

## Ce qui n'est pas couvert par `make deploy`

- **Nouvelle dépendance** (`requirements.txt`) ou changement du `Dockerfile` :
  `make deploy` reconstruit bien l'image, mais le téléchargement des paquets
  peut prendre plusieurs minutes.
- **Changement de `.env`** : pris en compte au prochain `make up` ou `make deploy`,
  sans nouvelle version. Un retour arrière ne restaure pas `.env`.
- **Changement de la structure de la base** : les colonnes ajoutées par
  `init_db()` restent en place après un `make rollback` ; une version plus
  ancienne doit les tolérer (c'est le cas tant qu'on ne fait qu'ajouter).
- **Icône et écran de démarrage Windows** : après modification de
  `scripts/windows/` ou de l'icône, relancer `make shortcut`.
