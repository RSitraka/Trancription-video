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
- [Structure du projet](#structure-du-projet)
- [Performances](#performances)
- [Tests](#tests)
- [Dépannage](#dépannage)
- [Feuille de route](#feuille-de-route)

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
mkdir -p media               # y déposer les fichiers à traiter
```

**CPU** (fonctionne partout) :

```bash
docker compose up -d --build
```

**GPU** (recommandé, ~15× plus rapide) :

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

---

## Composition des services

| Service | Rôle | Port |
|---|---|---|
| `api` | FastAPI : création de jobs, upload chunké, progression | `API_PORT` → 8000 |
| `worker` | Celery : FFmpeg + Whisper + compression | — |
| `cli` | Traitements ponctuels en ligne de commande (profil `cli`) | — |
| `postgres` | Métadonnées, état des chunks, reprise | — |
| `redis` | Courtier de messages Celery | — |

### Volumes

| Volume | Contenu | Pourquoi |
|---|---|---|
| `./media` → `/media` (ro) | fichiers sources | bind mount : pas de copie d'un fichier de 500 Go dans Docker |
| `data` → `/data` | chunks, sorties | volume nommé, survit à `docker compose down` |
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

Ouvrir <http://localhost:8100> (port `API_PORT`). Une page unique, servie par
FastAPI — ni Node, ni build, ni dépendance supplémentaire.

```
┌──────────────────────────────────────────────────────────┐
│  Transcription Vidéo & Audio                        ●    │
├──────────────────────────────────────────────────────────┤
│  Ajouter une vidéo                                       │
│  ┌────────────────────────────────────────────────────┐  │
│  │      Glisse un fichier ici ou clique pour parcourir│  │
│  │      envoyé par morceaux de 64 Mo — sans limite    │  │
│  └────────────────────────────────────────────────────┘  │
│  [Transcrire ▾]  [Détection auto ▾]  [SRT + JSON ▾]      │
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
- choisir le **traitement** (transcrire / compresser / les deux), la **langue**
  (détection automatique par défaut) et les **formats** de sortie ;
- suivre la **progression** de chaque travail, rafraîchie toutes les 2,5 s ;
- **télécharger** les sous-titres produits ou supprimer un travail.

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

# Compression seule — sans --target-gb, encodage CRF (qualité constante)
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

Exposée dès `docker compose up`, sur le port `API_PORT` (`.env`).

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/` | interface web |
| `GET` | `/api` | index JSON des routes |
| `GET` | `/health` | sonde de vie |
| `GET` | `/media` | fichiers présents dans `media/` |
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

`./source` et `main.py` sont montés en bind mount : le code modifié sur l'hôte
est vu immédiatement dans les conteneurs.

```bash
docker compose exec api bash            # shell dans le conteneur
docker compose restart worker           # recharger après modification
docker compose exec cli ffmpeg -version # FFmpeg de l'image
```

Reconstruire après ajout d'une dépendance dans `requirements.txt` :

```bash
docker compose build --no-cache api && docker compose up -d
```


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

## Formats de sortie

| Format | Contenu |
|---|---|
| `.txt` | texte brut, sans horodatage |
| `.srt` | sous-titres horodatés |
| `.vtt` | sous-titres WebVTT (lecture navigateur) |
| `.json` | segments + timestamps + confiance + OCR + vision |
| `.csv` | export tabulaire pour analyse |

---

## Structure du projet

```
transcription_video_audio/
├── Dockerfile              # image commune api / worker / cli
├── docker-compose.yml      # stack CPU (défaut)
├── docker-compose.gpu.yml  # override GPU (NVIDIA)
├── .dockerignore
├── .env.example            # à copier en .env
├── requirements.txt
├── main.py                 # point d'entrée CLI
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
    ├── db.py               # modèles SQLAlchemy (jobs, parts)
    ├── api.py              # FastAPI, upload chunké, progression
    ├── tasks.py            # workers Celery
    └── static/
        └── index.html      # interface web (page unique, sans build)
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

### Vérification complète

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
| Transcription dans une langue inattendue | `LANGUAGE` force une langue absente de la piste audio (doublage automatique) | `LANGUAGE=auto` ou `--lang auto` — la langue détectée est journalisée |
| Écriture refusée dans `/data` | UID différent sur un bind mount | `chown -R 1000:1000` du dossier côté hôte |

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
- [ ] Indexation vectorielle (Qdrant) pour la recherche sémantique
- [ ] Lecteur vidéo synchronisé avec la transcription dans l'interface
- [ ] Stockage objet S3 / MinIO pour les fichiers sources
- [ ] Image multi-arch (`linux/amd64`, `linux/arm64`) publiée sur GHCR
- [ ] Manifests Kubernetes pour un déploiement multi-GPU
