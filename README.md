# Transcription Vidéo & Audio

Pipeline Python de **transcription** et de **compression** de fichiers audio/vidéo
volumineux (500 Go à plusieurs To), conçu pour fonctionner **en flux** (streaming)
sans jamais charger le fichier complet en mémoire.

Le projet est **entièrement conteneurisé** : FFmpeg, CUDA, Whisper, Redis et
PostgreSQL vivent dans les images. Sur l'hôte, seul Docker est requis.

Le projet répond à deux besoins distincts, volontairement découplés :

| Objectif | Moyen |
|---|---|
| Vidéo/audio → texte horodaté | FFmpeg + faster-whisper |
| Réduire la taille du fichier | FFmpeg (H.265 / AV1, ré-encodage audio) |

---

## Sommaire

- [Principe](#principe)
- [Architecture](#architecture)
- [Prérequis](#prérequis)
- [Démarrage rapide](#démarrage-rapide)
- [Composition des services](#composition-des-services)
- [Interface web](#interface-web)
- [Utilisation](#utilisation)
- [Réduction de taille](#réduction-de-taille)
- [Traitement par chunks](#traitement-par-chunks)
- [Images explicatives (OCR / Vision)](#images-explicatives-ocr--vision)
- [Formats de sortie](#formats-de-sortie)
- [RAG : indexation et recherche](#rag--indexation-et-recherche)
- [Structure du projet](#structure-du-projet)
- [Performances](#performances)
- [Tests](#tests)
- [Dépannage](#dépannage)
- [Feuille de route](#feuille-de-route)
- [Documentation projet](#documentation-projet)

---

## Principe

Une erreur classique consiste à compresser la vidéo *avant* de la transcrire.
C'est inutile : la transcription ne dépend que de la **piste audio**, qui pèse
généralement moins de 1 % du fichier.

Le pipeline traite donc les deux branches indépendamment :

```
Fichier source (500 Go / 1 To)
        │
        ├──► extraction audio (16 kHz mono) ──► chunks ──► transcription
        │
        └──► compression vidéo (H.265/AV1) ──► fichier final allégé
```

Une vidéo de 500 Go contient presque toujours un débit vidéo excessif
(ProRes, DNxHD, H.264 haut débit). Un ré-encodage H.265 CRF 24 permet
couramment de descendre d'un facteur 5 à 20 sans perte visible.

---

## Architecture

```
              ┌─────────────────────┐
              │   Fichier vidéo     │
              │   500 Go / 1 To ... │
              └──────────┬──────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │      FFmpeg         │
              │ Extraction audio    │
              │ / découpage chunks  │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │    Chunks audio     │
              │   10 / 30 / 60 min  │
              └──────────┬──────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │   faster-whisper    │
              │    Transcription    │
              └──────────┬──────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │ Texte + timestamps  │
              │  SRT / VTT / JSON   │
              └─────────────────────┘
```

### Stack

```
Docker Compose
 ├── api      (image transcription-video-audio)
 │    └── FastAPI            → API + upload chunké
 ├── worker   (image transcription-video-audio, GPU)
 │    ├── FFmpeg             → extraction, découpage, compression
 │    ├── faster-whisper     → transcription (CTranslate2)
 │    ├── OpenCV             → détection de changement de scène
 │    └── Tesseract          → texte affiché à l'écran (optionnel)
 ├── redis    (redis:7)      → courtier Celery
 └── postgres (postgres:16)  → métadonnées, état des jobs, reprise
```

L'image applicative est construite sur Ubuntu 24.04 (Python 3.12) : base
`nvidia/cuda:12.9.2-cudnn-runtime` pour le GPU, `ubuntu:24.04` pour le CPU.
CTranslate2 (moteur de faster-whisper) demande **CUDA 12 + cuDNN 9** : ne pas
basculer sur une base CUDA 13, et rester sur une version ≥ 12.6, la première à
proposer des images Ubuntu 24.04.
Le même Dockerfile sert à l'API, aux workers et à la CLI — seul le `command`
diffère.

---

## Prérequis

Tout tourne en conteneur : **aucune dépendance à installer sur l'hôte** hormis
Docker. Ni Python, ni FFmpeg, ni CUDA toolkit.

- **Docker Engine 24+** et **Docker Compose v2**
- Pour l'accélération GPU : pilote NVIDIA 550+ et **NVIDIA Container Toolkit**
- Espace disque : prévoir **~1,5×** la taille du fichier source pour le volume `data`

```bash
docker --version && docker compose version

# Vérifier l'accès GPU depuis un conteneur (facultatif)
docker run --rm --gpus all nvidia/cuda:12.9.2-base-ubuntu24.04 nvidia-smi
```

---

## Démarrage rapide

```bash
git clone <url-du-depot> transcription_video_audio
cd transcription_video_audio

cp .env.example .env          # ajuster modèle, langue, taille cible
# Secrets obligatoires / conseillés (voir docs/SECURITE.md)
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^API_TOKEN=.*/API_TOKEN=$(openssl rand -hex 32)/" .env
mkdir -p media               # y déposer les fichiers à traiter
```

### Comme un logiciel Windows (WSL)

```bash
make shortcut   # icône « Transcription Vidéo » sur le Bureau et dans le menu Démarrer
```

Un double-clic sur l'icône :

1. affiche un **écran de démarrage** (sans console) qui suit l'avancement :
   Docker, services, carte graphique (le worker est redémarré s'il a perdu le GPU) ;
2. ouvre l'interface dans une **fenêtre d'application** — sans onglets ni barre
   d'adresse — avec le navigateur par défaut s'il est basé sur Chromium
   (Chrome, Edge, Brave, Vivaldi), sinon Edge ;
3. garde WSL actif en arrière-plan : les traitements continuent même fenêtre fermée.

En cas d'échec, l'écran de démarrage affiche le message, avec **Voir le détail**
(journal complet) et **Réessayer**.

Fichiers installés dans `%LOCALAPPDATA%\TranscriptionVideo` : l'écran de démarrage
(`TranscriptionVideo.hta`, généré depuis `scripts/windows/`), l'icône et le
journal du dernier démarrage. Relancer `make shortcut` après avoir déplacé le dépôt.
Depuis un terminal, sans écran de démarrage : `make launch`.

L'icône est générée par `scripts/make_icons.py` : `make icons` pour la régénérer.

### Déployer une modification

Le logiciel lancé par l'icône est une **version déployée** : son code est figé
dans l'image Docker, modifier les fichiers du dépôt ne le change pas.

```bash
make dev        # modifier en direct (code du dépôt monté, API rechargée)
make test       # vérifier
make deploy     # tests + nouvelle version + bascule ; retour automatique si échec
make rollback   # revenir à la version précédente
make versions   # version en service et historique
```

Détails : [docs/DEPLOIEMENT.md](docs/DEPLOIEMENT.md).

### Avec Make

```bash
make start    # démarre la version déployée (déploie d'abord au tout premier lancement)
make stop     # arrête la stack (les volumes et les sorties sont conservés)
```

Le GPU est détecté automatiquement (présence de `nvidia-smi`, y compris sous
WSL) : `make start` ajoute alors `docker-compose.gpu.yml`. Pour forcer un mode,
passer `GPU=0` (CPU) ou `GPU=1` (GPU) — et utiliser la même valeur pour
`make stop` :

```bash
make start GPU=0
make stop GPU=0
```

`make` ne fait qu'appeler `docker compose` : rien d'autre n'est installé sur
l'hôte.

### Avec Docker Compose directement

**CPU** (fonctionne partout) :

```bash
docker compose up -d --build
```

**GPU** (recommandé, ~15× plus rapide) — image taguée `:gpu`, distincte de la
variante CPU taguée `:latest` :

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Pour éviter de répéter les `-f`, exporter une fois pour toutes :

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
```

Contrôle :

```bash
docker compose ps
docker compose logs -f worker
curl localhost:8100/health          # {"status":"ok","version":"0.1.0"}
```

> Le port hôte est réglé par `API_PORT` dans `.env` (8100 par défaut, car 8000
> et 8080 sont souvent déjà occupés). Dans le conteneur, l'API écoute toujours
> sur 8000.
>
> L'API et Qdrant ne sont joignables **que depuis cette machine** (`127.0.0.1`) :
> l'API n'a pas d'authentification et permet de supprimer des vidéos.
> `BIND_ADDRESS=0.0.0.0` dans `.env` les ouvre au réseau local — voir
> [docs/SECURITE.md](docs/SECURITE.md) avant de le faire.

---

## Composition des services

| Service | Rôle | Port |
|---|---|---|
| `api` | FastAPI : création de jobs, upload chunké, progression | `API_PORT` → 8000 |
| `worker` | Celery : FFmpeg + Whisper + compression | — |
| `cli` | Traitements ponctuels en ligne de commande (profil `cli`) | — |
| `postgres` | Métadonnées, état des chunks, reprise | — |
| `redis` | Courtier de messages Celery | — |
| `qdrant` | Base vectorielle pour la recherche | `QDRANT_PORT` → 6333 |

### Volumes

| Volume | Contenu | Pourquoi |
|---|---|---|
| `./media` → `/media` (ro) | fichiers sources | bind mount : pas de copie d'un fichier de 500 Go dans Docker |
| `data` (ou `WORK_PATH`) → `/data` | piste audio extraite, morceaux, uploads | volume nommé, survit à `docker compose down` ; `WORK_PATH` le place sur un autre disque |
| `models` → `/models` | modèles Whisper (~3 Go) | évite un retéléchargement à chaque démarrage |
| `pgdata`, `redisdata` | état des jobs | reprise après redémarrage |

> Le premier lancement télécharge `large-v3` (~3 Go) dans le volume `models`.
> Les suivants démarrent immédiatement.

### Mise à l'échelle

Un worker = un modèle Whisper en mémoire. Pour paralléliser, on multiplie les
conteneurs, pas la concurrence interne :

```bash
docker compose up -d --scale worker=4
```

Sur GPU unique, garder `--scale worker=1` : plusieurs modèles `large-v3`
saturent la VRAM (~5 Go chacun en `float16`).

---

## Interface web

Ouvrir avec l'icône du Bureau, ou <http://localhost:8100> (port `API_PORT`).
Une page unique, servie par FastAPI — ni Node, ni build, ni dépendance
supplémentaire — organisée comme une application :

| Page | Contenu |
|---|---|
| **Tableau de bord** | chiffres clés (en cours, terminés, espace gagné), traitements en cours, derniers résultats |
| **Fichiers à traiter** | options (langue, taille maximale, découpage), vidéos du dossier source, envoi depuis le PC |
| **Traitements** | filtres, recherche, progression et temps restant, téléchargements |
| **Fichiers produits** | résultats groupés par vidéo, archive `.zip`, suppression |
| **Réglages** | thème (système, clair, sombre), notification Windows en fin de traitement, dossiers, raccourcis |

Un fichier peut être déposé n'importe où dans la fenêtre. Raccourcis :
<kbd>Alt</kbd>+<kbd>1</kbd>…<kbd>5</kbd> (pages), <kbd>Ctrl</kbd>+<kbd>O</kbd> (envoyer un fichier),
<kbd>Ctrl</kbd>+<kbd>F</kbd> (rechercher). La barre d'état indique la connexion,
les traitements en cours et la dernière actualisation.

**Aucune connexion n'est demandée** : l'interface n'est joignable que depuis ce
PC (`127.0.0.1`), elle ne répond qu'aux adresses `localhost` / `127.0.0.1`, et
refuse toute requête émise par un autre site web ouvert dans le navigateur.

### Accès depuis un autre PC du réseau

```bash
# .env
BIND_ADDRESS=0.0.0.0
REQUIRE_TOKEN=true                                   # obligatoire hors de cette machine
ALLOWED_HOSTS=localhost,127.0.0.1,::1,192.168.1.10,MON-SERVEUR   # IP et nom du serveur
```

puis `make up`. Sous WSL, le port n'existe que dans la machine virtuelle : une
redirection Windows et une règle de pare-feu sont nécessaires, à installer une
seule fois en administrateur —
`%LOCALAPPDATA%\TranscriptionVideo\acces-reseau.cmd` (posé par `make shortcut`,
source : `scripts/windows/`). Pour tout refermer : le même script avec `/retirer`.

L'interface s'ouvre alors sur `http://<ip-du-serveur>:8100` et demande le jeton
(`API_TOKEN` de `.env`) une fois par navigateur. Le trafic n'est pas chiffré :
à réserver à un réseau de confiance, sinon passer par un reverse proxy TLS.

Pour ouvrir l'accès au réseau (`BIND_ADDRESS=0.0.0.0`), activer d'abord le jeton :
`REQUIRE_TOKEN=true` dans `.env`, et ajouter le nom de la machine à
`ALLOWED_HOSTS`. L'interface demande alors le jeton (`API_TOKEN`) une fois par
navigateur ; l'icône du Bureau connecte automatiquement.

### Jeton d'accès

Le jeton est la ligne `API_TOKEN` du fichier `.env` du serveur (64 caractères).
Il n'est **jamais versionné** : `.env` est exclu de Git.

**L'obtenir**

```bash
# sur le serveur
grep API_TOKEN ~/transcription_video_audio/.env

# depuis un autre PC (SSH vers le serveur)
ssh <utilisateur>@<ip-du-serveur> "grep API_TOKEN ~/transcription_video_audio/.env"
```

S'il est vide, l'API en a généré un au démarrage :
`docker compose exec api cat /data/api_token`.

**L'utiliser** — une fois par navigateur, la session dure ensuite 30 jours :

- ouvrir `http://<ip-du-serveur>:8100` et le coller dans la fenêtre **Connexion**
  (la ligne entière `API_TOKEN=…` est acceptée aussi) ;
- ou ouvrir directement `http://<ip-du-serveur>:8100/#jeton=<jeton>` : la
  connexion est automatique, et le jeton est aussitôt effacé de la barre
  d'adresse et de l'historique ;
- en ligne de commande : en-tête `Authorization: Bearer <jeton>`.

Sur le serveur, l'icône du Bureau connecte toute seule.

**Le changer** — s'il a circulé, ou périodiquement :

```bash
cd ~/transcription_video_audio
sed -i "s/^API_TOKEN=.*/API_TOKEN=$(openssl rand -hex 32)/" .env
make up
grep API_TOKEN .env        # le nouveau jeton, à coller une fois par navigateur
```
ssh rasix@192.168.1.113 "grep API_TOKEN ~/transcription_video_audio/.env"
Toutes les sessions ouvertes sont alors déconnectées.

```
┌──────────────────────────────────────────────────────────┐
│  Transcription Vidéo & Audio                        ●    │
├──────────────────────────────────────────────────────────┤
│  Ajouter une vidéo                                       │
│  ┌────────────────────────────────────────────────────┐  │
│  │      Glisse un fichier ici ou clique pour parcourir│  │
│  │      envoyé par morceaux de 64 Mo — sans limite    │  │
│  └────────────────────────────────────────────────────┘  │
│  Langue : [Français | Anglais]   Taille max : [300] Mo   │
├──────────────────────────────────────────────────────────┤
│  Fichiers déjà présents dans media/                      │
│  cours.mp4                        0,68 Go     [Lancer]   │
├──────────────────────────────────────────────────────────┤
│  Travaux                                                 │
│  cours.mp4      transcription · 42 %    [SRT] [JSON] [×] │
│  ▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░                                   │
└──────────────────────────────────────────────────────────┘
```

Elle permet de :

- **déposer un fichier** par glisser-déposer — le navigateur le découpe en
  morceaux de 64 Mo envoyés un par un, avec 3 tentatives par morceau ; la
  mémoire du navigateur ne dépend donc pas de la taille du fichier ;
- **lancer un fichier déjà dans `media/`** sans le réenvoyer ;
- **supprimer une vidéo** (🗑) de `media/` : après une alerte de confirmation,
  la vidéo est effacée **définitivement** du disque avec toutes ses
  compressions, ses sous-titres et ses travaux (API : `DELETE /media?path=…`).
  Seul le service `api` monte `media/` en écriture ; `worker` et `cli` restent
  en lecture seule ;
- choisir la **langue de la vidéo** : **Français** ou **Anglais** (mémorisé
  par le navigateur). Chaque vidéo est d'abord **transcrite** dans cette langue
  (`.srt` + `.json`, pour le RAG), puis **compressée** ; sous-titres et parties
  vidéo sont rangés ensemble dans le dossier de sortie ;
- régler la **taille maximale** des fichiers compressés — **300 Mo par défaut**.
  Une vidéo trop longue pour tenir dans cette limite est découpée en plusieurs
  parties, chacune sous 300 Mo (case « découper » cochée) : `cours_compressed_1.mp4`,
  `cours_compressed_2.mp4`… (un seul fichier reste `cours_compressed.mp4`),
  rangées avec les sous-titres dans un dossier au nom de la vidéo :
  `output/cours/`. La valeur choisie est
  mémorisée par le navigateur : si une ancienne valeur (400) s'affiche, la
  remplacer une fois par 300 ;
- suivre la **progression** de chaque travail, rafraîchie toutes les 2,5 s ;
- **télécharger** les fichiers produits un par un, ou d'un coup avec
  **📦 Tout (.zip)** : une archive contenant un dossier au nom de la vidéo,
  avec toutes les parties et les sous-titres (API : `GET /jobs/{id}/archive.zip`,
  `GET /outputs/archive.zip?base=…`) ;
- voir et **supprimer tous les fichiers produits** (section « Fichiers
  produits ») : tout le contenu de `output/`, groupé par vidéo, y compris les
  fichiers dont le traitement a déjà été retiré de la liste — 🗑 par vidéo ou
  « Tout supprimer », avec alerte (API : `GET /outputs`, `DELETE /outputs`) ;
- **retirer un travail** (✕) en choisissant : *garder les fichiers* — relancer
  le même fichier reprend là où il s'était arrêté, les parties déjà faites ne
  sont pas refaites — ou *supprimer aussi les fichiers* produits du disque
  (API : `DELETE /jobs/{id}?files=true`).

Le thème suit celui du système (clair ou sombre).

Fichier : `source/static/index.html`. Il est monté en bind mount, donc modifiable
sans reconstruire l'image — un simple rechargement de la page suffit.

---

## Utilisation

### Ligne de commande

Le service `cli` est sous profil : il ne démarre pas avec la stack, on l'invoque
à la demande.

```bash
# Métadonnées du fichier (taille, durée, codecs, nombre de morceaux prévus)
docker compose run --rm cli python main.py info /media/cours.mp4

# Transcription seule
docker compose run --rm cli python main.py transcribe /media/cours.mp4 \
    --lang fr --formats srt,vtt,json

# Compression seule — fichiers de 300 Mo maximum (découpés en parties si besoin)
docker compose run --rm cli python main.py compress /media/cours.mp4 --max-mb 300

# Compression seule — sans --max-mb ni --target-gb, encodage CRF (qualité constante)
docker compose run --rm cli python main.py compress /media/cours.mp4 --target-gb 450

# Transcription puis compression
docker compose run --rm cli python main.py process /media/cours.mp4 \
    --target-gb 450 --formats srt,json
```

Options utiles : `--ocr` (analyse des diapositives), `--codec hevc_nvenc`
(encodeur matériel), `--out /data/out/mon-dossier`, `-v` (journal détaillé).

Les résultats atterrissent dans le volume `data`, sous `/data/out`. Pour les
récupérer sur l'hôte :

```bash
docker compose cp cli:/data/out ./out
# ou monter directement un dossier hôte dans docker-compose.override.yml
```

Une interruption (`Ctrl+C`, worker tué) ne perd rien : le plan de découpage et
les morceaux déjà transcrits sont sur disque, la reprise repart du morceau
suivant.

### API

Exposée dès `docker compose up`, sur le port `API_PORT` (`.env`), sans
authentification depuis ce PC. Les requêtes doivent viser `localhost` ou
`127.0.0.1` (`ALLOWED_HOSTS`). Avec `REQUIRE_TOKEN=true`, ajouter le jeton :

```bash
export TOKEN=$(grep '^API_TOKEN=' .env | cut -d= -f2)
alias api='curl -H "Authorization: Bearer $TOKEN"'   # puis remplacer curl par api
```

Limites d'upload : morceaux de 128 Mo au plus, total égal à la taille annoncée,
taille annoncée tenant dans l'espace disque libre (réglages
`UPLOAD_PART_MAX_MB`, `UPLOAD_FREE_MARGIN_MB`).

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/` | interface web |
| `GET` | `/api` | index JSON des routes |
| `GET` | `/health` | sonde de vie (sans jeton) |
| `POST` | `/login` | `{"token": "…"}` → cookie de session pour le navigateur |
| `POST` | `/logout` | fin de session |
| `GET` | `/media` | fichiers présents dans `media/` |
| `GET` | `/search` | recherche sémantique (`q`, `limit`, `source`) |
| `GET` | `/sources` | vidéos indexées dans Qdrant |
| `GET` | `/frames/{chemin}` | capture d'écran extraite |
| `POST` | `/jobs/{id}/index` | indexer la transcription dans Qdrant |
| `POST` | `/jobs` | créer un job (fichier local ou upload) |
| `PUT` | `/jobs/{id}/parts/{n}` | envoyer un morceau d'upload |
| `GET` | `/jobs/{id}/parts` | morceaux déjà reçus (reprise) |
| `POST` | `/jobs/{id}/complete` | assembler et mettre en file |
| `GET` | `/jobs/{id}` | statut et progression |
| `GET` | `/jobs/{id}/result.{ext}` | télécharger une sortie |
| `DELETE` | `/jobs/{id}` | supprimer le job et ses temporaires |

**Fichier déjà présent dans `./media`** — aucun upload nécessaire :

```bash
curl -X POST localhost:8100/jobs -H 'Content-Type: application/json' \
     -d '{"path": "/media/cours.mp4", "mode": "transcribe",
          "options": {"language": "fr", "formats": ["srt", "json"]}}'
```

**Upload chunké**, pour un fichier qui n'est pas sur la machine hôte :

```bash
# 1. Déclarer le job
curl -X POST localhost:8100/jobs -H 'Content-Type: application/json' \
     -d '{"filename": "cours.mp4", "size": 536870912000, "mode": "process"}'

# 2. Envoyer les morceaux (dans n'importe quel ordre, reprenable)
curl -X PUT localhost:8100/jobs/{id}/parts/0 --data-binary @part_00

# 3. Vérifier ce qui est arrivé avant de reprendre après une coupure
curl localhost:8100/jobs/{id}/parts       # {"received":[0,1],"bytes":226734}

# 4. Assembler et lancer le traitement
curl -X POST localhost:8100/jobs/{id}/complete
```

Suivi et récupération :

```bash
curl localhost:8100/jobs/{id}          # {"status":"transcribing","progress":0.427}
curl -O localhost:8100/jobs/{id}/result.srt
```

Documentation interactive : <http://localhost:8100/docs>

### Développement

Le logiciel en service utilise le code **figé** au dernier `make deploy`. Pour
travailler sur le code en direct :

```bash
make dev                                # code du dépôt monté, API rechargée à chaque modification
docker compose restart worker           # le worker, lui, se relance à la main
docker compose exec api bash            # shell dans le conteneur
make up                                 # retour à la version déployée
```

Une fois satisfait : `make test`, `git commit`, `make deploy`
(voir [docs/DEPLOIEMENT.md](docs/DEPLOIEMENT.md)). Après ajout d'une dépendance
dans `requirements.txt`, `make deploy` reconstruit aussi l'image.

---

## Réduction de taille

Le module de compression choisit un débit cible à partir de la durée du média
et de la taille demandée, puis encode en deux passes.

```python
# Débit vidéo cible (kbps) pour une taille voulue
bitrate = (target_size_bytes * 8) / duration_seconds / 1000 - audio_bitrate
```

Commande générée, exécutée dans le conteneur (FFmpeg n'est pas requis sur l'hôte) :

```bash
docker compose run --rm cli ffmpeg -i /media/source.mp4 \
  -c:v libx265 -crf 24 -preset medium \
  -c:a aac -b:a 128k \
  -movflags +faststart \
  /data/out/sortie.mp4
```

Avec le GPU, remplacer l'encodeur par `-c:v hevc_nvenc -cq 26` (le conteneur
`cli` reçoit la carte via `docker-compose.gpu.yml`).

Ordres de grandeur observés sur une source 4K ProRes :

| Codec source | Codec cible | Taille | Qualité |
|---|---|---|---|
| ProRes 422 | H.264 CRF 20 | ÷ 8 | visuellement identique |
| ProRes 422 | H.265 CRF 24 | ÷ 15 | très proche |
| H.264 haut débit | H.265 CRF 26 | ÷ 4 | proche |
| H.264 | AV1 (SVT-AV1) | ÷ 6 | proche, encodage lent |

> L'accélération matérielle (`hevc_nvenc`, `av1_nvenc`) est 5 à 20× plus rapide
> mais donne un fichier ~20 % plus gros à qualité équivalente. Attention :
> l'AV1 par NVENC exige une carte Ada (RTX 40xx) ; sur Ampère (RTX 30xx),
> s'en tenir à `hevc_nvenc`.

---

## Très gros fichiers (plusieurs To)

Un fichier de plusieurs To n'est **jamais copié** : il est lu sur place, en flux.
Ne pas l'envoyer depuis l'interface (refusé si l'espace manque) ni le copier
dans `media/`. Le laisser sur son disque (USB, NAS) et, dans `.env` :

```bash
MEDIA_PATH=/mnt/e/videos                 # où est le fichier
OUTPUT_PATH=/mnt/e/resultats             # parties compressées et sous-titres
WORK_PATH=/mnt/e/transcription-travail   # fichiers temporaires (sinon sur C:)
```

puis `make up`. Place à prévoir, par heure de vidéo :

| | Par heure | Exemple : 270 h (6 To de 4K H.264 à 50 Mbps) |
|---|---|---|
| Temporaire (`WORK_PATH`) : piste audio extraite | ~115 Mo | ~31 Go |
| Résultats (`OUTPUT_PATH`) : parties de 300 Mo, ≥ 1000 kbps | ~450 Mo | ~120 Go, ~420 fichiers |

- **Espace vérifié avant chaque étape** : si la piste audio ou les parties
  compressées ne tiennent pas, le traitement est refusé tout de suite, avec les
  Go nécessaires et disponibles, au lieu d'échouer après des heures de lecture.
- Au-delà de ~37 h d'audio (4 Go), la piste est écrite au format **RF64**, que
  le WAV classique ne sait pas dépasser.
- Une interruption (coupure, redémarrage, `make deploy` avec `FORCE=1`) ne fait
  rien perdre : morceaux transcrits et parties compressées sont repris.
- Durée : le fichier est lu au moins deux fois en entier (audio, puis
  compression) ; depuis un disque USB, compter 8 à 11 h par lecture de 6 To.
  Désactiver la mise en veille de Windows pendant le traitement.

## Traitement par chunks

À ne **jamais** faire :

```python
data = open("video_1TB.mp4", "rb").read()   # ✗ saturation mémoire immédiate
```

Le pipeline découpe et traite séquentiellement, chunk par chunk :

```
1 To de vidéo
     ↓  ffmpeg -vn -ac 1 -ar 16000  (extraction audio en flux)
audio.wav (~10 Go)
     ↓  ffmpeg -f segment -segment_time 1800
chunk_001 → transcription ─┐
chunk_002 → transcription ─┤
chunk_003 → transcription ─┼─► fusion + décalage des timestamps
   ...                     │
chunk_NNN → transcription ─┘
     ↓
transcription finale (SRT / VTT / JSON)
```

Points clés :

- **Recouvrement** de 2 s entre chunks pour ne pas couper un mot ; les segments
  dupliqués sont dédoublonnés à la fusion.
- **Décalage** : chaque timestamp local est additionné à l'offset du chunk.
- **Reprise** : l'état de chaque chunk (`pending` / `done` / `failed`) est
  persisté ; une interruption reprend là où elle s'est arrêtée.
- **Nettoyage** : les chunks sont supprimés dès leur transcription validée.

Extrait de transcription :

```python
from faster_whisper import WhisperModel

model = WhisperModel("large-v3", device="cuda", compute_type="float16")

segments, info = model.transcribe(
    "chunk_001.wav",
    language="fr",
    vad_filter=True,          # ignore les silences → gain de temps important
    beam_size=5,
)

for s in segments:
    print(f"{s.start:.2f} --> {s.end:.2f}: {s.text}")
```

---

## Images explicatives (OCR / Vision)

Pour les vidéos de cours, présentations ou démonstrations, l'audio seul est
insuffisant : les schémas, diapositives, tableaux et captures d'écran portent
une partie de l'information. Un module optionnel indexe aussi ce qui est
**affiché**.

```
Vidéo
  ├── Audio ──────────► faster-whisper ──► transcription vocale
  │
  └── Frames ─────────► OpenCV (détection de scène)
                           ├── OCR (PaddleOCR / Tesseract) ──► texte à l'écran
                           └── Vision (Qwen2.5-VL) ─────────► description du schéma
```

**Ne pas extraire 25 images/seconde** : 10 h de vidéo produiraient ~900 000
images quasi identiques. On échantillonne toutes les 2 s et on ne conserve que
les images dont le contenu a changé.

```python
import cv2

video = cv2.VideoCapture("cours.mp4")
fps = video.get(cv2.CAP_PROP_FPS)
frame_id, previous = 0, None

while True:
    ok, frame = video.read()
    if not ok:
        break

    if frame_id % int(fps * 2) == 0:            # 1 image toutes les 2 s
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if previous is None or cv2.absdiff(previous, gray).mean() > 18:
            cv2.imwrite(f"frames/frame_{frame_id:08d}.jpg", frame)
            previous = gray

    frame_id += 1
```

Les images retenues sont ensuite alignées sur les segments de parole par leur
timestamp, ce qui produit un enregistrement enrichi :

```json
{
  "start": 735.2,
  "end": 762.8,
  "speech": "Maintenant nous allons voir l'architecture complète du système RAG.",
  "ocr": ["Architecture RAG", "FastAPI", "Qdrant", "Embedding", "LLM"],
  "frame": "frames/frame_00018380.jpg",
  "description": "Schéma du flux Utilisateur → Frontend → FastAPI → Qdrant"
}
```

Le fichier devient alors une **base de connaissances interrogeable** : on peut
rechercher aussi bien ce qui a été dit que ce qui a été montré.

---

## Titre des dossiers de sortie

Un nom de fichier qui ne dit rien du contenu — `VID_20260918_141233.mp4`,
`WhatsApp Video 2026-09-18 at 14.12.33.mp4`, `enregistrement (2).mp4` — donne un
dossier de résultats tout aussi illisible. Le logiciel détecte ces noms
(appareils photo, téléphones, applications de visio, horodatages, identifiants)
et tire alors un **titre de la transcription** :

1. **les métadonnées** du fichier (balise `title`), quand l'outil d'export l'a écrite ;
2. **la parole** : la phrase d'annonce si le locuteur en fait une — « Dans cette
   vidéo, on va voir comment installer Docker sous Windows » → **« Installer
   Docker sous Windows »** — sinon les mots les plus présents au début ;
3. **le texte affiché à l'écran**, lu sur quelques images (OCR). C'est le seul
   recours pour une vidéo sans son : cours filmé, diaporama, capture d'écran.
   Le **plus gros texte** l'emporte, car c'est presque toujours le titre — un
   cours téléchargé sous le nom `videoplayback.mp4` devient
   **« JavaScript Course »** en quelques secondes ;
4. sinon, le nom du fichier est gardé : jamais de titre inventé.

Chaque étape n'est tentée que si la précédente n'a rien donné : l'OCR, le seul
coûteux, ne tourne que pour les vidéos sans parole exploitable.

Le titre nomme le dossier, les sous-titres et les parties compressées :
`output/Installer Docker sous Windows/Installer Docker sous Windows_compressed_1.mp4`.
Il est mémorisé avec le traitement, et l'interface affiche « titré d'après le
contenu » à côté du nom d'origine.

**Un nom de fichier déjà parlant n'est jamais réécrit** : `Réunion budget 2026.mp4`
reste tel quel, casse comprise. Détails : `source/titling.py`.

## Formats de sortie

| Format | Contenu |
|---|---|
| `.txt` | texte brut, sans horodatage |
| `.srt` | sous-titres horodatés |
| `.vtt` | sous-titres WebVTT (lecture navigateur) |
| `.json` | segments + timestamps + confiance + OCR + vision |
| `.rag.jsonl` | passages regroupés, prêts à indexer (une ligne = un passage) |
| `<video>_frames/` | captures d'écran extraites, si l'OCR est activé |
| `.csv` | export tabulaire pour analyse |

---

## RAG : indexation et recherche

Les transcriptions alimentent une base vectorielle **Qdrant**, interrogeable en
langage naturel depuis l'interface ou l'API.

### Ne rien perdre des images

Aucun format texte ne conserve les diapositives : **`.srt`, `.vtt`, `.txt` et
`.csv` ne retiennent que la parole**. Pour qu'une image reste exploitable, il
faut activer l'OCR :

```bash
ENABLE_OCR=true          # dans .env
# ou, ponctuellement :
docker compose run --rm cli python main.py transcribe /media/cours.mp4 \
    --ocr --formats rag,json
```

Trois choses sont alors produites :

| | |
|---|---|
| `<video>_frames/` | les captures elles-mêmes, à côté des transcriptions et non dans le dossier de travail purgé |
| `.json` | une liste `frames` exhaustive : horodatage, image, texte lu |
| `.rag.jsonl` | des passages `kind: "screen"` pour les diapositives **qu'aucune parole ne recouvre** |

Ce dernier point est le seul moyen de ne rien perdre : une diapositive montrée
en silence n'est rattachée à aucun segment de parole et disparaîtrait des
formats classiques. Elle devient ici un passage à part entière, indexé sur son
texte à l'écran.

```json
{"id": "cours#screen-cours_frames/frame_000735s.jpg", "kind": "screen",
 "text": "Architecture RAG FastAPI Qdrant Embedding",
 "timecode": "00:12:15.000", "frames": ["cours_frames/frame_000735s.jpg"]}
```

Les images sont servies par l'API, donc affichables dans les réponses du RAG :

```
GET /frames/cours_frames/frame_000735s.jpg
```

> **Réglage clé : `SCENE_THRESHOLD`** — c'est le pourcentage de pixels modifiés
> qui déclenche une capture. 1 convient aux diapositives (seul le texte change),
> 5 à 10 aux vidéos filmées, où un seuil bas capturerait chaque mouvement de
> caméra. La comparaison porte sur la proportion de pixels modifiés et non sur
> la moyenne des écarts : sur un fond blanc, un changement de titre ne fait
> bouger que quelques pour cent de l'image et passerait inaperçu autrement.

### Pourquoi un format dédié

Les segments Whisper durent 5 à 15 secondes — une phrase. Vectorisés tels quels,
ils perdent leur contexte et la recherche devient bruitée. Le format `rag`
regroupe donc les segments en passages d'environ 1200 caractères (~300 mots)
avec un recouvrement, et conserve les bornes temporelles :

```json
{"id": "cours#0007", "text": "…", "source": "cours", "chunk": 7,
 "start": 735.2, "end": 762.8, "timecode": "00:12:15.200", "segments": 5}
```

Le champ `timecode` est l'intérêt principal : une réponse du RAG peut citer la
minute exacte de la vidéo plutôt que le fichier entier.

| Réglage (`.env`) | Défaut | Rôle |
|---|---|---|
| `RAG_CHUNK_CHARS` | 1200 | taille visée d'un passage (512 tokens ≈ 2000 car.) |
| `RAG_OVERLAP_SEGMENTS` | 1 | segments repris d'un passage au suivant |
| `EMBEDDING_MODEL` | `intfloat/multilingual-e5-large` | multilingue : interroger en français un contenu espagnol |
| `QDRANT_COLLECTION` | `transcriptions` | collection Qdrant |

> Le code ajoute automatiquement les préfixes `passage:` et `query:` exigés par
> les modèles e5. Sans eux la pertinence chute nettement — c'est un piège
> classique de ces modèles.

### En ligne de commande

```bash
# Transcrire directement au format RAG
docker compose run --rm cli python main.py transcribe /media/cours.mp4 --formats srt,rag

# Ou regénérer le format depuis un JSON déjà produit, sans retranscrire
docker compose run --rm cli python main.py export /data/out/cours.json --formats rag

# Indexer, puis interroger
docker compose run --rm cli python main.py index /data/out/cours.rag.jsonl
docker compose run --rm cli python main.py search "comment créer un agent IA"
```

### Depuis l'interface

Page **Recherche** (Alt+5, ou Ctrl+K) : une question en langage naturel, et
des résultats affichant le timecode, la pertinence, le passage et les captures
d'écran quand l'OCR est activé. Une recherche peut aussi être ouverte
directement : `http://localhost:8100/?q=comment+créer+un+agent`.

Filtres, cumulables, qui relancent la recherche dès qu'ils changent :

| Filtre | Choix | API |
|---|---|---|
| Vidéo | toutes, ou une vidéo indexée | `source=` |
| Type | tout, **paroles** (ce qui a été dit), **à l'écran** (texte lu par OCR) | `kind=speech\|screen` |
| Pertinence | toutes, bonne (≥ 0,80), forte (≥ 0,85) | `min_score=` |
| Résultats | 5, 10, 20, 50 | `limit=` (1 à 50) |

Les pages **Fichiers à traiter** et **Fichiers produits** ont aussi un champ
« Filtrer par nom », affiché dès que la liste dépasse quelques éléments.

Le modèle de recherche (~2 Go) est téléchargé une seule fois, dans le volume
`models` (`/models/fastembed`) : il survit aux redémarrages et aux déploiements.
Pendant ce premier téléchargement, la page Recherche indique que le moteur se
prépare (`GET /search/ready`).

Pour rendre une vidéo cherchable : bouton **Indexer** sur un traitement terminé
(page **Traitements**). L'état s'affiche ensuite sur le bouton : nombre de
passages indexés, ou échec à réessayer.

> La toute première question télécharge le moteur de recherche
> (`intfloat/multilingual-e5-large`, ~2 Go) dans le volume `models` : compter
> quelques minutes. Les questions suivantes répondent en une fraction de seconde.

### Par l'API

```bash
curl -X POST localhost:8100/jobs/{id}/index
curl "localhost:8100/search?q=comment+créer+un+agent+IA&limit=5"
```

L'indexation tourne dans le processus de l'API, pas dans un worker Celery :
elle est courte et purement CPU, donc elle n'entre pas en concurrence avec le
GPU occupé par les transcriptions.

Les identifiants de points sont déterministes (`uuid5` de l'`id` du passage) :
réindexer un même fichier met à jour les points au lieu de les dupliquer.

### Console Qdrant

<http://localhost:6333/dashboard> — pour inspecter la collection, les vecteurs
et les charges utiles.

---

## Structure du projet

```
transcription_video_audio/
├── Makefile                # make start / make stop (GPU auto-détecté)
├── Dockerfile              # image commune api / worker / cli
├── docker-compose.yml      # stack CPU (défaut)
├── docker-compose.gpu.yml  # override GPU (NVIDIA)
├── docker-compose.dev.yml  # mode développement : code monté, rechargement (make dev)
├── .dockerignore
├── .env.example            # à copier en .env
├── requirements.txt
├── requirements-dev.txt    # pytest (étape « test » du Dockerfile uniquement)
├── pytest.ini
├── main.py                 # point d'entrée CLI
├── docs/
│   ├── SCRUM.md            # vision, backlog, sprints, définition de « terminé »
│   └── SECURITE.md         # modèle de menaces, protections, vulnérabilités
├── tests/                  # pytest : modules, API, sécurité (make test)
├── scripts/
│   ├── smoke_test.sh       # vérification de bout en bout sur la stack démarrée
│   ├── lancer.sh           # démarre la stack et ouvre l'interface (make launch)
│   ├── deployer.sh         # make deploy / rollback / versions
│   ├── installer-raccourci.sh  # installation Windows : Bureau + menu Démarrer (make shortcut)
│   ├── windows/TranscriptionVideo.hta  # écran de démarrage Windows (modèle)
│   └── make_icons.py       # génère l'icône du logiciel (make icons)
├── media/                  # fichiers sources, monté en /media (ro)
└── source/
    ├── config.py           # configuration (pydantic-settings)
    ├── media.py            # ffprobe, extraction audio, progression FFmpeg
    ├── chunker.py          # découpage, recouvrement, reprise
    ├── transcribe.py       # faster-whisper, fusion des segments
    ├── compress.py         # calcul de débit, CRF ou deux passes
    ├── frames.py           # détection de scène + OCR (optionnel)
    ├── export.py           # SRT / VTT / JSON / TXT / CSV
    ├── pipeline.py         # orchestration, partagée CLI ↔ workers
    ├── rag.py              # indexation et recherche Qdrant
    ├── db.py               # modèles SQLAlchemy (jobs, parts)
    ├── api.py              # FastAPI, upload chunké, progression
    ├── tasks.py            # workers Celery
    └── static/
        ├── index.html      # interface web (page unique, sans build)
        ├── manifest.webmanifest  # nom et icônes de l'application
        └── icon.ico, icon-*.png  # icône : onglet, raccourci, écran d'accueil
```

---

## Performances

**Mesuré** sur RTX 3060 (12 Go), `large-v3` en `float16`, VAD activé, sur
5 minutes d'un cours vidéo :

| Étape | Débit |
|---|---|
| Transcription GPU (RTX 3060) | **2,3× temps réel** (128 s pour 300 s d'audio) |
| Chargement du modèle | ~15 s (après le téléchargement initial de 3 Go) |

Soit **~2 h 30 pour une vidéo de 6 heures**. Repères indicatifs pour le reste :

| Étape | Débit approximatif |
|---|---|
| Extraction audio | ~200× temps réel (limité par le disque) |
| Transcription CPU (8 cœurs) | ~2× temps réel |
| Compression H.265 CPU `medium` | ~0,5× temps réel |
| Compression `hevc_nvenc` | ~8× temps réel |

> Les chiffres de 20 à 30× temps réel qu'on lit souvent supposent une carte
> haut de gamme **et** l'inférence par lots (`BatchedInferencePipeline` de
> faster-whisper), non utilisée ici : le pipeline transcrit séquentiellement,
> ce qui est plus simple à reprendre après interruption.

---

## Tests

Deux niveaux, tous deux exécutés dans Docker :

```bash
make test       # tests automatisés : modules, API, sécurité (~10 s, sans GPU ni service)
make test-all   # make test, puis la vérification de bout en bout ci-dessous
```

### Tests automatisés (pytest)

`make test` construit l'étape `test` du Dockerfile (image de production
inchangée) et lance `pytest` sur le dépôt monté en lecture seule. Les tests
sont autonomes : SQLite remplace PostgreSQL, Celery, Whisper et Qdrant sont
simulés ; seul **FFmpeg est réel**, sur des clips de quelques secondes générés
à la volée — découpage, compression sous taille et extraction audio sont donc
vérifiés sur de vrais fichiers.

| Fichier | Couvre |
|---|---|
| `test_media.py` | ffprobe, exécution FFmpeg, extraction audio, parcours de dossiers |
| `test_chunker.py` | plan de découpage, recouvrement, reprise |
| `test_transcribe.py` | fusion des morceaux, cache, reprise, langue, mode par lots |
| `test_export.py` | SRT, VTT, TXT, JSON, CSV, passages RAG et diapositives muettes |
| `test_compress.py` | débits, plan de coupe, découpage sans ré-encodage, encodage sous taille |
| `test_pipeline.py` | dossiers de sortie, enchaînement, progression |
| `test_frames.py` | détection de scène, alignement image / parole, OCR indisponible |
| `test_rag.py` | indexation, identifiants déterministes, recherche filtrée |
| `test_config_db_tasks.py` | configuration, modèle de données, workers Celery |
| `test_cli.py` | ligne de commande |
| `test_api.py` | toutes les routes : jobs, upload chunké, zip, suppression, RAG |
| `test_security.py` | protections et vulnérabilités connues — voir [docs/SECURITE.md](docs/SECURITE.md) |

```bash
make test ARGS="tests/test_api.py -v"             # un seul fichier
make test ARGS="-k zip"                           # par mot-clé
make test ARGS="--cov=source --cov-report=term"   # couverture
```

Résultat attendu : tout passe, sauf les tests `test_vuln*` affichés **`xfailed`** —
ils décrivent des vulnérabilités connues, pas encore corrigées.

### Vérification de bout en bout

Un script parcourt toute la chaîne — services, CLI, workers, API, upload chunké —
sur un clip généré à la volée, puis nettoie derrière lui :

```bash
./scripts/smoke_test.sh
```

```
1. Services            ✓ conteneurs  ✓ API  ✓ worker  ✓ postgres  ✓ ffmpeg
2. Clip de test        ✓ génération
3. Ligne de commande   ✓ info  ✓ transcribe  ✓ SRT  ✓ JSON  ✓ compress  ✓ taille réduite
4. API (fichier local) ✓ création  ✓ traitement  ✓ téléchargement
5. Upload chunké       ✓ 3 morceaux  ✓ assemblage  ✓ traitement
6. Nettoyage           ✓
Résultat : 19 réussis, 0 échec
```

Il utilise le modèle `tiny` (~75 Mo) pour rester rapide. Pour valider la
configuration réelle, ou la pile GPU :

```bash
WHISPER_MODEL=large-v3 ./scripts/smoke_test.sh

COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml ./scripts/smoke_test.sh
```

> Le clip de test est une mire avec une tonalité pure : le VAD écarte tout
> l'audio, donc **0 segment et un SRT vide sont le résultat attendu**. Ce test
> valide la mécanique du pipeline, pas la qualité de la reconnaissance.

### Vérifications manuelles

```bash
# Le GPU est-il vu depuis le conteneur ?
docker compose -f docker-compose.yml -f docker-compose.gpu.yml \
    run --rm cli nvidia-smi

# CTranslate2 voit-il la carte ?
docker compose -f docker-compose.yml -f docker-compose.gpu.yml run --rm cli \
    python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"

# Sur un vrai fichier, en commençant par les métadonnées
docker compose run --rm cli python main.py info /media/cours.mp4

# Transcrire les 2 premières minutes seulement, pour juger la qualité
docker compose run --rm cli sh -c \
    "ffmpeg -y -i /media/cours.mp4 -t 120 -c copy /data/tmp/extrait.mp4 && \
     python main.py transcribe /data/tmp/extrait.mp4 --lang fr"
```

### Ce que le test ne couvre pas

- la **qualité** de transcription (il faudrait un enregistrement de parole réel) ;
- l'OCR des diapositives (`ENABLE_OCR=true` sur une vidéo contenant du texte) ;
- le comportement sur plusieurs centaines de Go — testable en réduisant
  `CHUNK_DURATION` pour forcer un grand nombre de morceaux.

---

## Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| `not found` sur l'image `nvidia/cuda:...` | tag inexistant : CUDA n'a des images Ubuntu 24.04 qu'à partir de **12.6.0** | garder un tag publié (`12.9.2-cudnn-runtime-ubuntu24.04`) — vérifier avec `docker manifest inspect` |
| `could not select device driver "nvidia"` | NVIDIA Container Toolkit absent ou non configuré | `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| `Unable to load libcudnn_ops.so` | image CPU utilisée avec `WHISPER_DEVICE=cuda` | ajouter `-f docker-compose.gpu.yml` et rebuild |
| `CUDA out of memory` | plusieurs workers sur un seul GPU | `--scale worker=1`, ou `WHISPER_MODEL=medium` |
| `No space left on device` | volume `data` saturé par les chunks | `DELETE_CHUNKS_AFTER=true`, `docker system prune --volumes` (⚠ efface les volumes non utilisés) |
| Modèle retéléchargé à chaque run | volume `models` non monté | vérifier `docker volume ls` et le montage `/models` |
| Fichier introuvable dans le conteneur | chemin hôte au lieu du chemin conteneur | utiliser `/media/...`, pas `./media/...` |
| « aucune piste audio » sur un fichier vidéo | flux vidéo seul (téléchargement YouTube « videoplayback », DASH) ou vidéo muette | rien à transcrire ; en mode *transcrire + compresser*, la compression se fait quand même et le traitement l'indique. Pour la parole, retélécharger la vidéo avec sa piste audio |
| Transcription dans une langue inattendue | `LANGUAGE` force une langue absente de la piste audio (doublage automatique) | `LANGUAGE=auto` ou `--lang auto` — la langue détectée est journalisée |
| `required variable POSTGRES_PASSWORD is missing a value` | secret absent de `.env` | ajouter `POSTGRES_PASSWORD=…` ; sur une base existante, aussi `docker exec <conteneur postgres> psql -U transcription -c "ALTER USER transcription PASSWORD '…'"` |
| `401 authentification requise` | `REQUIRE_TOKEN=true` et jeton absent ou changé | se reconnecter dans l'interface ; en ligne de commande, en-tête `Authorization: Bearer $TOKEN` |
| `403 nom d'hôte non autorisé` | interface ouverte par une autre adresse que `localhost` (IP réseau, nom de machine) | utiliser `http://localhost:8100`, ou ajouter le nom à `ALLOWED_HOSTS` (avec `REQUIRE_TOKEN=true` si ouvert au réseau) |
| Écriture refusée dans `/data` | UID différent sur un bind mount | `chown -R 1000:1000` du dossier côté hôte |
| L'icône du raccourci reste blanche ou ancienne | cache d'icônes de Windows | relancer `make shortcut`, sinon se déconnecter / reconnecter à Windows |
| Écran de démarrage : « Docker ne répond pas » | service Docker arrêté dans WSL | `sudo systemctl enable --now docker` |
| Écran de démarrage bloqué ou en erreur | voir **Voir le détail** | journal : `%LOCALAPPDATA%\TranscriptionVideo\demarrage.log` |
| L'interface s'ouvre dans un onglet et non dans une fenêtre | aucun navigateur Chromium trouvé | installer ou réparer Edge |
| `cuInit(0) failed` ou `GPU access blocked by the operating system` dans le worker | accès GPU perdu par un conteneur resté démarré (fréquent sous WSL après une mise en veille) | `docker compose -f docker-compose.yml -f docker-compose.gpu.yml restart worker` |

Diagnostic rapide :

```bash
docker compose logs -f worker
docker compose exec worker nvidia-smi
docker compose exec worker python -c "import torch, ctranslate2; print(ctranslate2.get_cuda_device_count())"
docker compose exec worker df -h /data
```

---

## Feuille de route

- [ ] Inférence par lots (`BatchedInferencePipeline`) : 3 à 4× plus rapide
- [ ] Diarisation (identification des locuteurs)
- [ ] Traduction automatique des sous-titres
- [ ] Lecteur vidéo synchronisé avec la transcription dans l'interface
- [ ] Stockage objet S3 / MinIO pour les fichiers sources
- [ ] Image multi-arch (`linux/amd64`, `linux/arm64`) publiée sur GHCR
- [ ] Manifests Kubernetes pour un déploiement multi-GPU

Backlog détaillé et priorisé : [docs/SCRUM.md](docs/SCRUM.md).

---

## Documentation projet

| Document | Contenu |
|---|---|
| [docs/SCRUM.md](docs/SCRUM.md) | vision produit, rôles, cérémonies, backlog, sprints, définition de « terminé » |
| [docs/SECURITE.md](docs/SECURITE.md) | modèle de menaces, protections en place, vulnérabilités connues et plan de correction |
| [docs/DEPLOIEMENT.md](docs/DEPLOIEMENT.md) | version déployée, mode développement, `make deploy` / `make rollback` |
