# Sécurité

Analyse de sécurité du projet **Transcription Vidéo & Audio** : périmètre,
menaces, protections en place, vulnérabilités connues et plan de correction.

| | |
|---|---|
| Version analysée | `0.1.0` — branche `compression-400mo` (commit `54c4be6`), puis correctifs VULN-01 à VULN-06 |
| Date | 2026-09-17 |
| Tests associés | `tests/test_security.py` (lancés par `make test`) |
| Prochaine revue | à chaque sprint, pendant la revue de sprint (voir [SCRUM.md](SCRUM.md)) |

---

## Sommaire

- [1. Périmètre et hypothèses](#1-périmètre-et-hypothèses)
- [2. Architecture et surfaces d'attaque](#2-architecture-et-surfaces-dattaque)
- [3. Actifs à protéger](#3-actifs-à-protéger)
- [4. Modèle de menaces (STRIDE)](#4-modèle-de-menaces-stride)
- [5. Protections en place](#5-protections-en-place)
- [6. Vulnérabilités connues](#6-vulnérabilités-connues)
- [7. Plan de correction](#7-plan-de-correction)
- [8. Recommandations de déploiement](#8-recommandations-de-déploiement)
- [9. Tests de sécurité](#9-tests-de-sécurité)
- [10. Procédure en cas d'incident](#10-procédure-en-cas-dincident)

---

## 1. Périmètre et hypothèses

**Dans le périmètre** : l'API FastAPI (`source/api.py`), l'interface web
(`source/static/index.html`), les workers Celery, la CLI, les images Docker et
les fichiers Compose.

**Hors périmètre** : la sécurité de l'hôte Docker, du pilote NVIDIA, du réseau
local, et les failles internes à FFmpeg, Whisper ou Qdrant (suivies par leurs
éditeurs — voir §8 pour les mettre à jour).

**Hypothèse d'usage actuelle** : outil **mono-utilisateur, sur un poste de
travail**, utilisé par une personne de confiance. L'application **n'a pas été
conçue pour être exposée** sur un réseau partagé ou sur Internet. Plusieurs
vulnérabilités ci-dessous ne deviennent graves qu'en sortant de ce cadre ;
leur gravité initiale a été évaluée **en supposant un accès depuis le réseau
local**, car c'était la configuration par défaut avant SEC-09 (ports publiés
sur `0.0.0.0`), et c'est toujours possible avec `BIND_ADDRESS=0.0.0.0`.

---

## 2. Architecture et surfaces d'attaque

```
             Réseau local / navigateur
                       │
       ┌───────────────┼──────────────────────┐
       │ :8100 (API_PORT, 127.0.0.1)  :6333 (QDRANT_PORT, 127.0.0.1)
       ▼                                      ▼
  ┌─────────┐   Celery   ┌─────────┐     ┌─────────┐
  │   api   │──────────► │  redis  │     │ qdrant  │
  │ FastAPI │            └────┬────┘     └─────────┘
  └────┬────┘                 │
       │  SQL            ┌────▼────┐  FFmpeg / Whisper
       ▼                 │ worker  │──────────────► fichiers média
  ┌──────────┐           └─────────┘
  │ postgres │
  └──────────┘

  Montages : media/ (rw pour api, ro pour worker/cli), output/ (rw),
             ./source et main.py (rw, code monté à chaud)
```

| Surface | Exposition | Entrées non fiables |
|---|---|---|
| API HTTP (`api`) | port hôte `API_PORT`, `127.0.0.1` par défaut (`BIND_ADDRESS`), jeton exigé | chemins, noms de fichier, corps d'upload, paramètres de requête |
| Interface web | servie par l'API, connexion par jeton | données renvoyées par l'API (noms de fichiers, erreurs) |
| Qdrant | port hôte `QDRANT_PORT`, `127.0.0.1` par défaut | API REST Qdrant complète, sans clé |
| Fichiers média | déposés dans `media/` ou envoyés | contenu binaire analysé par FFmpeg, OpenCV, Tesseract |
| PostgreSQL, Redis | réseau Docker interne uniquement | — |

---

## 3. Actifs à protéger

| Actif | Besoin | Impact d'une compromission |
|---|---|---|
| Vidéos sources (`MEDIA_PATH`, jusqu'à plusieurs To) | intégrité, disponibilité | **perte définitive** : `DELETE /media` efface sans corbeille |
| Transcriptions et index RAG | confidentialité | contenu de cours / réunions divulgué |
| Code monté (`./source`, `main.py`) | intégrité | exécution de code arbitraire dans l'API et les workers |
| Hôte (disque, GPU) | disponibilité | disque saturé, GPU monopolisé |

---

## 4. Modèle de menaces (STRIDE)

| Catégorie | Menace | Statut |
|---|---|---|
| **S**poofing | N'importe quel client agit comme l'utilisateur légitime | ⚠ maîtrisé pour un poste mono-utilisateur — ports locaux (SEC-09), hôte et origine vérifiés (SEC-14) ; jeton en option (SEC-10) pour une machine partagée |
| **T**ampering | Écriture de fichier hors du dossier prévu via le nom d'upload | ✅ corrigé — SEC-08 |
| **T**ampering | Traversée de chemin sur les routes de lecture / suppression | ✅ maîtrisé — SEC-01 à SEC-04 |
| **R**epudiation | Aucune trace de *qui* a supprimé une vidéo | ⚠ partiel — un seul jeton partagé, journaux sans identité |
| **I**nformation disclosure | Lecture de fichiers hors `output/` | ✅ maîtrisé — SEC-03 |
| **I**nformation disclosure | Lecture / suppression de l'index Qdrant par le réseau | ✅ corrigé — SEC-09 (clé d'API Qdrant encore absente) |
| **I**nformation disclosure | Chemins internes dans les messages d'erreur | ⚠ faible — [VULN-08](#vuln-08--messages-derreur-détaillés), réservé aux clients authentifiés |
| **D**enial of service | Upload sans limite de taille, saturation du disque | ✅ corrigé — SEC-11 |
| **D**enial of service | Requêtes coûteuses (`limit` illimité) | ✅ corrigé — SEC-12 |
| **E**levation of privilege | Évasion du conteneur | ⚠ atténué — utilisateur non root (SEC-07), durcissement incomplet ([VULN-07](#vuln-07--durcissement-incomplet-des-conteneurs)) |
| **E**levation of privilege | Fichier média piégé exploitant FFmpeg | ⚠ atténué — non root, dépendant des mises à jour (§8) |

---

## 5. Protections en place

Chaque protection est vérifiée par au moins un test de `tests/test_security.py`.

### SEC-01 — Confinement des chemins sources

`POST /jobs` n'accepte un `path` que s'il se trouve, **après résolution**
(`Path.resolve()`, qui suit les liens symboliques et élimine les `..`), sous
`MEDIA_PATH` ou le dossier de travail (`_allowed`, `source/api.py`).
Un chemin refusé renvoie **403 avant tout test d'existence** : l'API ne révèle
pas si un fichier existe ailleurs sur le disque.

*Tests* : `test_job_path_outside_allowed_roots`, `test_job_path_dotdot_escape`,
`test_job_path_symlink_escape`, `test_error_on_forbidden_path_does_not_reveal_existence`.

### SEC-02 — Dossier de sortie fixé par le serveur

L'option `subdir` envoyée par un client est **supprimée** (`options.pop("subdir")`) ;
seul le serveur la calcule. En défense en profondeur, `pipeline._inside` refuse
tout sous-dossier qui sortirait de `OUTPUT_DIR`.

*Tests* : `test_client_cannot_choose_output_subdir`, `test_pipeline_refuses_escaping_subdir`.

### SEC-03 — Lecture limitée au dossier de sortie

- `GET /frames/{name}` résout le chemin et vérifie qu'il reste sous `OUTPUT_DIR`
  (y compris via lien symbolique) ;
- `GET /jobs/{id}/archive.zip` écarte toute sortie hors de `OUTPUT_DIR` ;
- `GET /outputs/archive.zip` et `DELETE /outputs` ne parcourent que des dossiers
  résolus sous `OUTPUT_DIR`, et le nom de base est échappé (`re.escape`) avant
  d'être utilisé dans une expression régulière — `base=".*"` ne correspond à rien.

*Tests* : `test_frames_traversal`, `test_frames_symlink_escape`,
`test_job_archive_ignores_outputs_outside_output_dir`,
`test_outputs_archive_traversal`, `test_delete_outputs_cannot_escape`.

### SEC-04 — Suppression des sources confinée à `MEDIA_PATH`

`DELETE /media` refuse la racine de `MEDIA_PATH` elle-même, tout chemin résolu
en dehors (y compris le dossier de travail, un lien symbolique ou un chemin
relatif). Seul le service `api` monte `media/` en écriture ; `worker` et `cli`
le montent **en lecture seule** : un worker compromis ne peut pas effacer les sources.

*Tests* : `test_delete_media_confinement`, `test_delete_media_symlink_does_not_follow_outside`,
`test_worker_and_cli_mount_media_read_only`.

### SEC-05 — Archives `.zip` sûres

Les noms d'entrées sont assainis (`_windows_name`) : ni `/`, `\`, `:`, `..`
exploitables, ni caractères de contrôle. Une archive téléchargée ne peut pas
écrire hors de son dossier à l'extraction (*zip slip*). L'en-tête
`Content-Disposition` est encodé (RFC 5987), sans injection d'en-tête possible.

*Test* : `test_zip_entries_cannot_escape_on_extraction`.

### SEC-06 — Validation des entrées et requêtes inter-sites

- `mode` limité à `transcribe | compress | process` ; `status` de `DELETE /jobs`
  limité à `done | failed` par expression régulière ;
- tout accès à la base passe par l'ORM SQLAlchemy (requêtes paramétrées) : pas
  d'injection SQL par les identifiants de job ;
- les appels FFmpeg passent une **liste d'arguments** à `subprocess` (jamais
  `shell=True`) : un nom de fichier ne peut pas injecter de commande ;
- `POST /jobs` exige un corps JSON : un formulaire d'un site tiers (`text/plain`)
  est refusé (422). Les routes `PUT` / `DELETE` nécessitent une requête de
  pré-vérification CORS, et **aucun en-tête CORS n'est émis** : un site tiers
  ouvert dans le navigateur ne peut pas les déclencher. Les `POST` sans corps
  (`/jobs/{id}/complete`, `/jobs/{id}/index`) restent des requêtes « simples »,
  mais exigent un identifiant de job aléatoire de 128 bits qu'un site tiers ne
  peut ni deviner ni lire ;
- l'interface échappe toute donnée serveur insérée en HTML (`esc()`), ou passe
  par `textContent`.

*Tests* : `test_clear_jobs_status_is_whitelisted`, `test_mode_is_whitelisted`,
`test_job_id_injection_is_harmless`, `test_json_body_required_blocks_simple_csrf`,
`test_no_permissive_cors`, `test_ui_escapes_server_data`.

### SEC-07 — Conteneurs et configuration

- les processus tournent sous l'utilisateur **`app` (UID 1000)**, jamais root ;
- PostgreSQL et Redis **ne publient aucun port** sur l'hôte ;
- `.env` est exclu de Git et du contexte de construction Docker : aucun
  réglage local n'est intégré à l'image ;
- `pytest` et ses dépendances vivent dans une étape `test` séparée : l'image de
  production n'en contient pas.

*Tests* : `test_dockerfile_runs_as_non_root`, `test_internal_services_not_published`,
`test_env_file_never_shipped`.

### SEC-08 — Nom de fichier d'upload (correctif de VULN-01)

`POST /jobs` refuse (400) tout `filename` qui n'est pas un **nom simple** :
séparateur `/` ou `\`, caractère nul, `.` ou `..`, nom vide ou de plus de
255 octets, valeur non textuelle (`_upload_name`, `source/api.py`). Les noms
réels — espaces, accents, deux-points, parenthèses, caractères non latins —
restent acceptés. `/complete` **revérifie** le nom et s'assure que la
destination résolue est bien dans `sources/<job>/` : un job créé avant le
correctif, avec un nom dangereux déjà en base, est refusé lui aussi.

*Tests* : `test_upload_filename_cannot_escape`, `test_upload_filename_rejected`,
`test_upload_filename_simple_names_accepted`, `test_complete_rechecks_legacy_job_filename`.

### SEC-09 — Ports publiés sur la boucle locale (correctif de VULN-05)

L'API et Qdrant sont publiés sur `127.0.0.1` par défaut
(`"${BIND_ADDRESS:-127.0.0.1}:${API_PORT}:8000"`) : ils ne sont joignables
que depuis la machine qui exécute Docker. Cela ferme l'accès réseau à Qdrant
(VULN-05) et **réduit** VULN-03 à un attaquant déjà présent sur la machine.
`BIND_ADDRESS=0.0.0.0` dans `.env` rouvre l'accès au réseau local : à ne faire
qu'avec les précautions du §8.

*Test* : `test_ports_bound_to_localhost_by_default`.

### SEC-10 — Jeton d'accès, en option (correctif de VULN-03)

> **Désactivé par défaut depuis SEC-14** : sur un poste mono-utilisateur, la
> saisie du jeton gênait l'usage. Il s'active avec `REQUIRE_TOKEN=true`, et
> devient **obligatoire** dès que l'API est ouverte au réseau (`BIND_ADDRESS=0.0.0.0`).

Avec `REQUIRE_TOKEN=true`, toutes les routes exigent le jeton `API_TOKEN`, à l'exception de la page
d'accueil (qui ne contient aucune donnée), de `/health`, `/login` et `/logout`
(`source/auth.py`). La vérification est une **dépendance globale** de
l'application : une route ajoutée plus tard est protégée d'office.

- **ligne de commande** : en-tête `Authorization: Bearer <jeton>` ;
- **navigateur** : la première visite ouvre une fenêtre de connexion ;
  `POST /login` pose un cookie `HttpOnly`, `SameSite=Strict`, valable 30 jours,
  que les liens de téléchargement envoient aussi. Le cookie contient une
  **dérivée HMAC** du jeton, jamais le jeton ; changer `API_TOKEN` invalide
  toutes les sessions ;
- **icône du Bureau** : le lanceur ouvre `/#jeton=…`. Le fragment (`#`) n'est
  jamais envoyé au serveur ; l'interface l'efface de la barre d'adresse et de
  l'historique (`history.replaceState`) avant d'appeler `/login`. Il reste
  visible un court instant dans la ligne de commande du navigateur lancé par
  Windows — acceptable pour un poste mono-utilisateur ;
- comparaisons en temps constant (`hmac.compare_digest`) ; jeton jamais accepté
  dans les paramètres d'URL (`?`), qui finiraient dans les journaux ;
- `API_TOKEN` vide : un jeton aléatoire de 256 bits est créé une fois dans
  `/data/api_token` (droits `600`, volume persistant) et son emplacement journalisé.

Le cookie `SameSite=Strict` n'est envoyé ni par un site tiers ni vers un autre
nom d'hôte : il neutralise aussi le *DNS rebinding* décrit en VULN-10 pour
toutes les routes protégées.

*Tests* : `test_routes_require_token`, `test_every_route_is_protected_or_listed_public`,
`test_destructive_route_without_token_deletes_nothing`, `test_wrong_credentials`,
`test_token_in_query_string_is_ignored`, `test_login_sets_strict_http_only_cookie`,
`test_forged_cookie_rejected`, `test_changing_token_revokes_sessions`,
`test_generated_token_is_persistent_and_private`, `test_ui_asks_for_login_on_401`,
`test_ui_auto_login_clears_token_from_address_bar`.

### SEC-16 — Liens vidéo : pas d'accès au réseau interne

`POST /jobs` accepte un lien (`url`) que le worker télécharge. Sans contrôle,
c'est une **requête côté serveur** (SSRF) : un lien vers `http://qdrant:6333`,
`http://127.0.0.1:…` ou une adresse de métadonnées cloud ferait lire au serveur
des services qui ne sont pas exposés.

- seuls `http` et `https` sont acceptés ;
- le nom d'hôte est résolu, et **toutes** ses adresses doivent être publiques
  (`ipaddress.is_global`) : boucle locale, réseaux privés, lien local, CGNAT et
  adresses non routables sont refusés ;
- le contrôle est fait à la création du job (400) **et** au moment du
  téléchargement (le DNS peut avoir changé entre-temps) ;
- l'URL ne peut venir que du champ `url` : une option `url` glissée dans
  `options` d'un job ordinaire est retirée ;
- la taille est bornée par l'espace libre (`max_filesize`), avec une marge.

*Limite acceptée* : une redirection HTTP suivie par yt-dlp n'est pas revérifiée ;
le risque reste réservé aux clients autorisés (mode local ou jeton, SEC-10/14).

*Tests* : `tests/test_download.py` (liens refusés, adresses internes, double
résolution, nom inconnu), `test_link_refused_before_queueing`,
`test_url_cannot_be_smuggled_in_options`.

### SEC-15 — Accès réseau : jeton obligatoire, hôtes déclarés

Ouvrir l'API au réseau (`BIND_ADDRESS=0.0.0.0`) impose :

- `REQUIRE_TOKEN=true` — sans quoi n'importe quelle machine du réseau pourrait
  supprimer les vidéos (SEC-10) ;
- l'IP et le nom du serveur dans `ALLOWED_HOSTS` — toute autre valeur de `Host`
  reste refusée (SEC-14), ce qui conserve la protection contre le DNS rebinding ;
- sous WSL, une redirection Windows et une règle de pare-feu explicites
  (`scripts/windows/acces-reseau.ps1`), donc une ouverture consciente et
  réversible (`/retirer`).

**Limite acceptée** : le trafic est en clair (HTTP), jeton compris. À réserver à
un réseau de confiance ; pour un usage plus large, placer un reverse proxy TLS
devant l'API (VULN-03, §6).

### SEC-14 — Mode local sans connexion (défaut)

Choix du 17/09/2026 : pas de connexion depuis le poste lui-même. Cela reste sûr
pour un poste mono-utilisateur grâce à trois barrières (`auth.check_local`,
appliquée à toutes les routes, y compris en mode jeton) :

1. **réseau** : ports publiés sur `127.0.0.1` uniquement (SEC-09) ;
2. **nom d'hôte** : seuls `ALLOWED_HOSTS` (`localhost`, `127.0.0.1`, `::1`) sont
   servis (**403** sinon). Une page web qui ferait pointer son propre domaine
   sur 127.0.0.1 (*DNS rebinding*) garde son nom de domaine dans `Host` : refusée ;
3. **origine** : toute requête qui modifie quelque chose (`POST`, `PUT`,
   `DELETE`) avec un en-tête `Origin` étranger est refusée, ainsi que toute
   requête marquée `Sec-Fetch-Site: cross-site` par le navigateur — sauf la
   simple navigation vers la page (lien, favori). Un site ouvert dans un autre
   onglet ne peut ni lire ni supprimer quoi que ce soit.

**Risque accepté** : un programme ou un autre compte **du même PC** peut appeler
l'API (et donc supprimer des vidéos). C'est le périmètre d'un outil local ; si
la machine est partagée, activer `REQUIRE_TOKEN=true`.

*Tests* : `test_local_mode_is_the_default`, `test_local_mode_needs_no_login`,
`test_local_mode_rejects_foreign_host`, `test_local_mode_accepts_local_hosts`,
`test_allowed_hosts_can_be_extended`, `test_local_mode_rejects_cross_site_writes`,
`test_local_mode_accepts_same_origin_writes`, `test_local_mode_rejects_cross_site_reads`,
`test_local_mode_allows_navigation_from_elsewhere`, `test_token_mode_still_checks_host`.

### SEC-11 — Uploads bornés (correctif de VULN-02)

- `size` doit être un entier d'octets positif, et tenir dans l'espace libre du
  volume de travail en laissant `UPLOAD_PART_MAX_MB` + `UPLOAD_FREE_MARGIN_MB`
  (128 Mo + 1 Go par défaut) — sinon **507** ;
- un morceau est refusé (**413**) s'il dépasse 128 Mo ou si le total reçu
  dépasserait la taille annoncée. La limite s'applique **pendant la réception**,
  même sans `Content-Length` ; le morceau est écrit sous un nom temporaire, donc
  un envoi refusé n'écrase pas un morceau valide ;
- numéro de morceau entre 0 et la taille annoncée (100 000 au plus), sinon **400** ;
- plus aucun morceau accepté après `/complete` (**409**) ; `/complete` refuse un
  envoi incomplet (total reçu ≠ taille annoncée).

*Tests* : `test_declared_size_*`, `test_part_larger_than_declared`,
`test_total_of_parts_bounded`, `test_streamed_part_without_length_is_cut`,
`test_part_bounded_by_max_part_size`, `test_part_index_bounded`,
`test_no_part_after_completion`, `test_incomplete_upload_is_not_queued`.

*Limites restantes* : plusieurs uploads déclarés en même temps ne se
réservent pas l'espace les uns aux autres, et un upload abandonné n'est pas
purgé automatiquement (le retirer depuis l'interface supprime ses morceaux).
Risque faible : il faut désormais le jeton pour envoyer.

### SEC-12 — Pagination bornée (correctif de VULN-04)

`GET /jobs?limit=` est borné entre 1 et 10 000 (**422** au-delà). L'interface
demande 10 000 travaux pour afficher un dossier entier : la borne la couvre.

*Tests* : `test_jobs_limit_bounded`, `test_ui_limit_within_bound`.

### SEC-13 — Aucun mot de passe par défaut (correctif de VULN-06)

`POSTGRES_PASSWORD` est **obligatoire** : Compose refuse de démarrer s'il manque
dans `.env` (`${POSTGRES_PASSWORD:?…}`), et `DATABASE_URL` est construite à
partir de la même variable. Le code et `.env.example` ne contiennent plus aucun
mot de passe ni jeton. Sur une base déjà créée, changer la variable ne suffit
pas : voir la commande `ALTER USER` dans `.env.example`.

*Tests* : `test_postgres_password_required_from_env`, `test_no_default_secrets_in_code_or_example`.

---

## 6. Vulnérabilités connues

Échelle : **Critique** (exécution de code, perte de données sans interaction),
**Élevée**, **Moyenne**, **Faible**. Une vulnérabilité découverte et non encore
corrigée reçoit un test `test_vuln*` marqué `xfail(strict=True)` : il passera au
rouge dès que la correction sera livrée, pour rappeler de retirer le marqueur
et de mettre ce document à jour. Les descriptions des failles corrigées sont
conservées pour mémoire.

| ID | Titre | Gravité initiale | Statut |
|---|---|---|---|
| VULN-01 | Écriture de fichier arbitraire par le nom d'upload | Critique | ✅ corrigé — SEC-08 |
| VULN-02 | Taille d'upload non bornée | Élevée | ✅ corrigé — SEC-11 |
| VULN-03 | Aucune authentification ni contrôle d'accès | Élevée | ✅ corrigé — SEC-09, SEC-10 |
| VULN-05 | Qdrant publié sans authentification | Élevée | ✅ corrigé — SEC-09 (clé d'API conseillée) |
| VULN-06 | Identifiants PostgreSQL par défaut | Moyenne | ✅ corrigé — SEC-13 |
| VULN-04 | Pagination non bornée | Faible | ✅ corrigé — SEC-12 |
| VULN-07 | Durcissement incomplet des conteneurs | Moyenne | ❌ ouvert — revue de configuration |
| VULN-09 | Dépendances et images non épinglées | Moyenne | ❌ ouvert — revue de configuration |
| VULN-08 | Messages d'erreur détaillés | Faible | ❌ ouvert |
| VULN-10 | Pas d'en-têtes de sécurité HTTP ; DNS rebinding | Faible | ⚠ atténué — cookie `SameSite=Strict` (SEC-10) |

### VULN-01 — Écriture de fichier arbitraire par le nom d'upload

**✅ Corrigé le 17/09/2026 — voir SEC-08.** Description conservée pour mémoire.

**Gravité : Critique** · `source/api.py`, `complete_upload`

Le champ `filename` de `POST /jobs` est stocké tel quel, puis utilisé pour
construire la destination de l'assemblage :

```python
destination = settings.work_dir / "sources" / job_id / filename
```

- `filename = "../../evil.mp4"` écrit dans `/data/evil.mp4` ;
- `filename = "/app/source/tasks.py"` : en Python, `Path("/a") / "/b"` vaut
  `/b` — le chemin absolu **remplace** tout le préfixe.

**Scénario d'exploitation** : `./source` est monté **en écriture** dans le
conteneur `api` et appartient à l'UID 1000, le même que l'utilisateur `app`.
Un client du réseau déclare un upload nommé `/app/source/frames.py`, envoie du
code Python, puis appelle `/complete`. Le fichier source du projet est remplacé
**sur l'hôte**. Le prochain import de ce module (import paresseux dans un
worker, redémarrage, `--max-tasks-per-child`) exécute le code de l'attaquant
dans l'API et les workers, avec accès aux vidéos, à la base et au GPU.

**Correction retenue** (`_upload_name`) : refus explicite plutôt que nettoyage
silencieux, à la création et à l'assemblage. Principe :

```python
name = Path(filename).name            # ne garder que le dernier composant
if name in ("", ".", "..") or name != filename:
    raise HTTPException(400, "nom de fichier invalide")
destination = (settings.work_dir / "sources" / job_id / name).resolve()
if not destination.is_relative_to((settings.work_dir / "sources" / job_id).resolve()):
    raise HTTPException(400, "nom de fichier invalide")
```

À valider **dès `POST /jobs`** (refuser le job) et à revérifier dans `/complete`.
Reste à faire en complément (VULN-07) : monter `./source` et `main.py` en
lecture seule hors développement (`./source:/app/source:ro`).

### VULN-02 — Taille d'upload non bornée

**✅ Corrigé le 17/09/2026 — voir SEC-11.**

**Gravité : Élevée** · `upload_part`, `complete_upload`

La taille `size` déclarée n'était jamais contrôlée : un morceau peut être
arbitrairement gros, et le nombre de morceaux (`index`, entier libre, y compris
négatif) n'est pas limité. Un client peut remplir le volume `data`, ce qui fait
échouer toutes les transcriptions et compressions en cours.

**Correction prévue** : refuser (413) un morceau au-delà d'une taille maximale
(ex. 128 Mo) en coupant le flux dès le dépassement ; refuser `index < 0` ou
supérieur à `ceil(size / taille_morceau)` ; au `/complete`, vérifier que la
somme reçue égale `size` ; vérifier l'espace libre (`shutil.disk_usage`) avant
d'accepter un upload ; purger les uploads inachevés après 24 h.

### VULN-03 — Aucune authentification ni contrôle d'accès

**✅ Corrigé le 17/09/2026 — voir SEC-09 et SEC-10.**

**Gravité : Élevée** · toute l'API

Toutes les routes, y compris destructives (`DELETE /media`, `DELETE /outputs?all=true`,
`DELETE /jobs`), étaient ouvertes à quiconque atteint le port `API_PORT`. Avant
SEC-09, ce port était publié sur **toutes les interfaces** : tout appareil du
réseau local (Wi-Fi partagé, autre machine du bureau) pouvait effacer
définitivement plusieurs To de vidéos. Limité ensuite à `127.0.0.1`, il
restait accessible à un autre compte ou processus de la machine, ou à une page
web exploitant le *DNS rebinding* (VULN-10).

**Correction**, par ordre de simplicité :

1. ✅ **fait** (SEC-09) : API publiée sur la boucle locale par défaut ;
2. ✅ **fait** (SEC-10) : jeton d'API exigé par une dépendance FastAPI
   globale, en-tête pour les scripts, cookie de session pour le navigateur ;
3. **si exposition réseau** : reverse proxy (Caddy, Traefik) avec TLS et
   authentification (OIDC / SSO), et journal d'audit des suppressions.

### VULN-05 — Qdrant publié sans authentification

**✅ Corrigé le 17/09/2026 — voir SEC-09.** Reste conseillé : clé d'API.

**Gravité : Élevée** · `docker-compose.yml`

Qdrant était publié sur `0.0.0.0:6333` sans clé d'API : toute machine du réseau
peut lire l'intégralité des transcriptions indexées, les modifier ou supprimer
la collection. L'API n'a pourtant besoin que du réseau Docker interne.

**Correction** : retirer la publication du port, ou la limiter à
`"127.0.0.1:${QDRANT_PORT:-6333}:6333"` pour garder la console locale ; définir
`QDRANT__SERVICE__API_KEY` et le transmettre à `QdrantClient(api_key=…)`.

### VULN-06 — Identifiants PostgreSQL par défaut

**✅ Corrigé le 17/09/2026 — voir SEC-13.** L'ancien mot de passe reste lisible
dans l'historique Git : il doit être changé sur toute base existante.

**Gravité : Moyenne** · `docker-compose.yml`, `.env.example`, `source/config.py`

Le mot de passe `transcription` était écrit en dur dans Compose, dans l'exemple
de configuration et comme valeur par défaut du code, et figure dans
l'historique Git (fichier `.env` versionné avant le commit `6a580e3`). Le port
n'est pas publié, ce qui limite le risque à un attaquant déjà présent sur le
réseau Docker (comme le permettait VULN-01, désormais corrigée).

**Correction retenue** : `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?…}`, URL
construite depuis la même variable, suppression de la valeur par défaut dans
`config.py`. Amélioration possible : secrets Docker (`secrets:`) plutôt que
variables d'environnement.

### VULN-07 — Durcissement incomplet des conteneurs

**Gravité : Moyenne** · `docker-compose.yml`, `Dockerfile`

L'utilisateur est non root (SEC-07), mais :

- `build-essential` et `python3-dev` restent dans l'image finale (compilateur
  disponible pour un attaquant, surface et taille accrues) ;
- ni `security_opt: [no-new-privileges:true]`, ni `cap_drop: [ALL]`, ni
  système de fichiers en lecture seule (`read_only: true` + `tmpfs`) ;
- aucune limite de ressources (`mem_limit`, `pids_limit`) : un fichier piégé
  peut épuiser la mémoire de l'hôte ;
- `./source` monté en écriture dans tous les services (c'est ce qui rendait VULN-01 critique).

**Correction** : construction multi-étapes (compiler les roues dans une étape
`builder`, ne copier que `/opt/venv`), options de durcissement ci-dessus, et un
fichier `docker-compose.dev.yml` réservé au montage du code à chaud.

### VULN-08 — Messages d'erreur détaillés

**Gravité : Faible** · `api.py` (`search`, `list_sources`, `delete_media`), `tasks.py`

Les exceptions sont renvoyées au client (`f"recherche indisponible : {error}"`)
et stockées dans `Job.error`, révélant chemins internes, URL de services et
versions. **Correction** : message générique côté client, détail uniquement
dans les journaux.

### VULN-09 — Dépendances et images non épinglées

**Gravité : Moyenne** · `requirements.txt`, `docker-compose.yml`

`requirements.txt` n'utilise que des bornes basses (`>=`) : deux constructions
à des dates différentes n'installent pas le même code, et une version
compromise publiée sur PyPI serait intégrée automatiquement. Les images de
base sont référencées par tag, pas par empreinte.

**Correction** : fichier de verrouillage avec empreintes
(`pip-compile --generate-hashes` ou `uv pip compile`, installé avec
`--require-hashes`), images épinglées par `@sha256:…`, mise à jour outillée
(Dependabot / Renovate) et analyse de vulnérabilités (`pip-audit`,
`docker scout cves` ou `trivy image`) en intégration continue.

### VULN-04 — Pagination non bornée

**✅ Corrigé le 17/09/2026 — voir SEC-12** (borne à 10 000, valeur utilisée par
l'interface). Le parcours récursif de `MEDIA_PATH` reste recalculé à chaque appel.

**Gravité : Faible** · `GET /jobs?limit=`

`limit` acceptait n'importe quel entier : une seule requête charge toute la table
et appelle `stat()` sur chaque sortie. De même, `GET /media` parcourt
récursivement tout `MEDIA_PATH` à chaque appel.
**Correction** : `limit: int = Query(50, ge=1, le=500)` ; mise en cache courte
du parcours de `MEDIA_PATH`.

### VULN-10 — En-têtes de sécurité HTTP et DNS rebinding

**Gravité : Faible** · `source/api.py` · **atténué** : le cookie de session
`SameSite=Strict` (SEC-10) n'est pas envoyé à un nom d'hôte rebondi, et une page
tierce ne connaît pas le jeton ; les routes protégées ne sont donc plus
atteignables par ce biais. Restent les en-têtes absents.

Ni `Content-Security-Policy`, ni `X-Content-Type-Options`, ni
`X-Frame-Options` / `frame-ancestors` ; l'en-tête `Host` n'est pas vérifié. Une
page malveillante peut, par *DNS rebinding*, faire passer ses requêtes pour
des requêtes de même origine vers `localhost:8100` et contourner la protection
CORS de SEC-06. **Correction** : `TrustedHostMiddleware(allowed_hosts=["localhost", "127.0.0.1"])`
et un middleware ajoutant les en-têtes (le JavaScript de l'interface étant
inline, prévoir un `nonce` ou le déplacer dans un fichier `.js`).

---

## 7. Plan de correction

Priorisé dans le backlog produit ([SCRUM.md](SCRUM.md), épopée **E7 — Sécurité**).

| Priorité | Action | Corrige | Effort |
|---|---|---|---|
| ✅ P0 — fait | Refuser les noms d'upload qui ne sont pas des noms simples | VULN-01 | 1 pt |
| ✅ P0 — fait | Publier API et Qdrant sur `127.0.0.1` par défaut | VULN-03 (partiel), VULN-05 | 1 pt |
| ✅ P1 — fait | Borner taille et nombre de morceaux, contrôler l'espace disque | VULN-02 | 3 pts |
| ✅ P1 — fait | Jeton d'API sur toutes les routes, connexion dans l'interface | VULN-03 | 5 pts |
| ✅ P1 — fait | Mot de passe PostgreSQL obligatoire | VULN-06 | 2 pts |
| ✅ P3 — fait | `limit` borné | VULN-04 | 1 pt |
| P1 | Clé d'API Qdrant | VULN-05 (défense en profondeur) | 1 pt |
| P2 | Image multi-étapes, `no-new-privileges`, `cap_drop`, limites, code en `:ro` | VULN-07 | 3 pts |
| P2 | Verrouillage des dépendances, `pip-audit` et `trivy` en CI | VULN-09 | 3 pts |
| P2 | Purge des uploads abandonnés, réservation d'espace entre uploads | SEC-11 (limites) | 2 pts |
| P3 | Erreurs génériques, en-têtes HTTP, `TrustedHost` | VULN-08, 10 | 2 pts |

---

## 8. Recommandations de déploiement

**Poste de travail (usage prévu)** — configuration par défaut : `BIND_ADDRESS`
absent ou égal à `127.0.0.1`, et deux secrets dans `.env` :

```bash
echo "API_TOKEN=$(openssl rand -hex 32)"         >> .env   # si absent
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" >> .env   # si absent
# Base déjà créée : aligner le mot de passe réel sur la nouvelle valeur
docker compose exec postgres psql -U transcription \
    -c "ALTER USER transcription PASSWORD '<valeur de POSTGRES_PASSWORD>'"
make stop && make start
```

Vérification :

```bash
docker compose ps --format '{{.Service}} {{.Ports}}'   # 127.0.0.1:8100->8000/tcp
```

**Exposition réseau** (`BIND_ADDRESS=0.0.0.0`) — le jeton circule alors sur le
réseau : ne le faire que derrière TLS.

- reverse proxy TLS (Caddy, Traefik) devant l'API, idéalement avec une
  authentification nominative (OIDC / SSO) pour tracer qui supprime quoi ; ni Qdrant, ni Postgres, ni Redis exposés ;
- sauvegarde de `MEDIA_PATH` : `DELETE /media` est irréversible par conception ;
- pare-feu hôte n'autorisant que le proxy.

**Hygiène continue** :

```bash
make test                                                  # tests, dont sécurité
docker compose build --pull --no-cache                     # correctifs FFmpeg / CUDA / Python
docker compose --profile test run --rm test pip-audit      # après ajout de pip-audit à requirements-dev.txt
```

- ne jamais versionner `.env` ; régénérer les secrets s'ils l'ont été ;
- changer `API_TOKEN` en cas de doute : toutes les sessions sont invalidées ;
- traiter les fichiers média reçus de tiers comme non fiables : FFmpeg et
  OpenCV ont régulièrement des CVE sur des fichiers malformés — garder les
  images à jour est la principale défense.

---

## 9. Tests de sécurité

```bash
make test                                   # toute la suite
make test ARGS="tests/test_security.py -v"  # sécurité seule
```

Résultat attendu : **tous les tests passent**, sans `xfail` — les vulnérabilités
encore ouvertes (VULN-07 à 10) relèvent de la configuration et de la revue, pas
d'un comportement testable de l'API.

Pour une nouvelle faille : écrire le test du comportement attendu, le marquer
`xfail(strict=True)` et l'inscrire en §6. Quand la correction est livrée, le
test passe en `XPASS(strict)` et **fait échouer la suite** : retirer le
marqueur, décrire la protection en §5 et mettre à jour la date en tête de document.
Pour voir le détail d'échec des failles ouvertes :

```bash
make test ARGS="tests/test_security.py -k vuln --runxfail --tb=line"
```

Contrôles complémentaires non automatisés, à effectuer à chaque version :

- [ ] `docker compose config` : aucun port publié sur `0.0.0.0` ;
- [ ] `curl -s -o /dev/null -w '%{http_code}' localhost:8100/jobs` renvoie `401` ;
- [ ] `git log -p -- .env` : aucun secret réel dans l'historique ;
- [ ] `trivy image transcription-video-audio:latest` : aucune CVE critique corrigeable ;
- [ ] revue manuelle des nouvelles routes : chemin résolu et confiné, entrée validée.

---

## 10. Procédure en cas d'incident

1. **Isoler** : `make stop` (les volumes sont conservés pour l'analyse).
2. **Préserver** : `docker compose logs --no-color > incident-$(date +%F).log` ;
   copie de `pgdata` (table `jobs` : noms de fichiers, dates, erreurs).
3. **Vérifier l'intégrité du code** : `git status` et `git diff` — toute
   modification inattendue de `source/` ou `main.py` peut indiquer une
   exploitation de VULN-01 antérieure au correctif ; chercher aussi dans la
   table `jobs` des `filename` contenant `/` ou `..`.
4. **Changer les secrets** : nouvel `API_TOKEN` (invalide toutes les
   sessions), nouveau `POSTGRES_PASSWORD` (`ALTER USER`, voir §8).
5. **Évaluer** les données touchées : `media/`, `output/`, collection Qdrant.
6. **Corriger** puis **reconstruire** : `docker compose build --no-cache`,
   changer les mots de passe, restaurer depuis la sauvegarde.
7. **Documenter** l'incident et ajouter un test de non-régression dans
   `tests/test_security.py`.
