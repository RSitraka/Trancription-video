# Scrum — Transcription Vidéo & Audio

Cadre de travail Scrum du projet : vision, rôles, cérémonies, backlog produit,
historique des sprints et définition de « terminé ».

| | |
|---|---|
| Dépôt | `github.com/RSitraka/Trancription-video` |
| Durée d'un sprint | **2 semaines** (10 jours ouvrés) |
| Sprint en cours | **Sprint 2** — 14/09/2026 → 25/09/2026 |
| Unité d'estimation | points d'effort (suite de Fibonacci : 1, 2, 3, 5, 8, 13) |
| Mise à jour | à chaque revue de sprint |

> **Mise en place au sprint 2.** Les sprints 1 et 2 sont reconstitués à partir
> de l'historique Git (dates et contenu des commits) ; les estimations, horaires
> et rôles sont des propositions à valider par l'équipe lors de la prochaine
> planification.

---

## Sommaire

- [1. Vision produit](#1-vision-produit)
- [2. Équipe et rôles](#2-équipe-et-rôles)
- [3. Cérémonies](#3-cérémonies)
- [4. Artefacts](#4-artefacts)
- [5. Définition de « prêt » et de « terminé »](#5-définition-de-prêt-et-de-terminé)
- [6. Backlog produit](#6-backlog-produit)
- [7. Historique des sprints](#7-historique-des-sprints)
- [8. Sprint 3 — proposition](#8-sprint-3--proposition)
- [9. Risques et impédiments](#9-risques-et-impédiments)
- [10. Indicateurs](#10-indicateurs)

---

## 1. Vision produit

> **Pour** les formateurs, équipes pédagogiques et archivistes qui possèdent des
> centaines de Go de vidéos de cours et de réunions,
> **qui** doivent les rendre consultables, partageables et interrogeables,
> **Transcription Vidéo & Audio** est un outil auto-hébergé
> **qui** transcrit, compresse sous une taille garantie et indexe ces vidéos
> pour la recherche en langage naturel,
> **contrairement aux** services en ligne facturés à la minute, limités en taille
> et qui exigent d'envoyer les contenus à un tiers,
> **notre produit** traite sur place des fichiers de plusieurs To, reprend après
> interruption et ne dépend que de Docker.

**Objectifs mesurables**

| Objectif | Indicateur | Cible |
|---|---|---|
| Traiter de très gros volumes | taille de fichier source supportée | ≥ 2 To, mémoire constante |
| Tenir une taille de dépôt imposée | parties compressées sous la limite | 100 % sous `max_mb` (300 Mo par défaut) |
| Rester rapide sur GPU | vitesse de transcription RTX 3060 | ≥ 2× temps réel |
| Rendre les contenus retrouvables | réponse RAG avec timecode exact | ≥ 1 passage pertinent dans le top 5 |
| Ne rien perdre | reprise après arrêt du worker | aucune partie terminée refaite |

---

## 2. Équipe et rôles

| Rôle | Titulaire | Responsabilités |
|---|---|---|
| **Product Owner** | *à désigner* | porte la vision, ordonne le backlog, accepte ou refuse les éléments en revue |
| **Scrum Master** | *à désigner* | anime les cérémonies, lève les impédiments, protège le sprint |
| **Équipe de développement** | RSitraka | conçoit, développe, teste, documente et livre l'incrément |
| **Parties prenantes** | utilisateurs formateurs, exploitants de la machine GPU | expriment les besoins, participent à la revue |

> Dans une équipe d'une personne, les trois rôles peuvent être cumulés ; il est
> alors d'autant plus important de tenir la revue et la rétrospective, même
> courtes, et de faire valider les priorités par un utilisateur réel.

---

## 3. Cérémonies

| Cérémonie | Quand | Durée max. | Entrées → sorties |
|---|---|---|---|
| **Planification de sprint** | lundi J1, 9 h 30 | 2 h | backlog ordonné, vélocité → objectif de sprint, backlog de sprint |
| **Mêlée quotidienne** | chaque jour, 9 h 30 | 15 min | tableau → plan du jour, impédiments |
| **Affinage du backlog** | mercredi S1 | 1 h | nouvelles demandes → éléments « prêts », estimés |
| **Revue de sprint** | vendredi J10, 14 h | 1 h | incrément démontré (`make start`) → backlog mis à jour |
| **Rétrospective** | vendredi J10, 15 h 15 | 45 min | ressenti, faits → 1 à 3 actions d'amélioration |

**Mêlée quotidienne** — trois questions, tournées vers l'objectif de sprint :
qu'ai-je fait hier pour l'objectif ? que vais-je faire aujourd'hui ? qu'est-ce
qui me bloque ?

**Revue de sprint** — ordre du jour type :

1. rappel de l'objectif de sprint ;
2. démonstration sur la stack réelle : `make start`, puis dépôt d'une vidéo dans l'interface ;
3. résultat de `make test-all` et de la revue de sécurité ([SECURITE.md](SECURITE.md)) ;
4. éléments non terminés, et pourquoi ;
5. réordonnancement du backlog avec les parties prenantes.

**Rétrospective** — format « Garder / Arrêter / Commencer », les actions
retenues sont ajoutées au backlog du sprint suivant.

---

## 4. Artefacts

| Artefact | Où | Engagement associé |
|---|---|---|
| **Backlog produit** | [§6](#6-backlog-produit) de ce document (ou GitHub Projects) | vision produit (§1) |
| **Backlog de sprint** | [§7](#7-historique-des-sprints) | objectif de sprint |
| **Incrément** | branche fusionnée dans `main`, images Docker construites | définition de « terminé » (§5) |

**Tableau** : colonnes `À faire` → `En cours` → `En revue` → `Terminé`.
Limite de travail en cours : **2** éléments dans `En cours`.

**Branches** : une branche par élément ou par objectif de sprint
(ex. `compression-400mo`), fusionnée dans `main` par pull request une fois la
définition de « terminé » respectée.

---

## 5. Définition de « prêt » et de « terminé »

### Définition de « prêt » (avant d'entrer dans un sprint)

- [ ] rédigé sous la forme « En tant que… je veux… afin de… » ;
- [ ] critères d'acceptation vérifiables ;
- [ ] estimé par l'équipe, 8 points maximum (sinon découper) ;
- [ ] dépendances identifiées (GPU, service externe, données de test) ;
- [ ] impact sécurité évalué : nouvelle entrée utilisateur ? nouveau chemin de fichier ?

### Définition de « terminé »

- [ ] les critères d'acceptation sont satisfaits et démontrés ;
- [ ] **`make test` passe** : tests unitaires, API et sécurité (aucun échec, `xfail` uniquement pour les vulnérabilités connues) ;
- [ ] tests ajoutés pour le nouveau comportement, dont un test de sécurité pour toute nouvelle route manipulant un chemin ;
- [ ] **`./scripts/smoke_test.sh` passe** sur la stack démarrée (CPU ; GPU si la fonctionnalité le concerne) ;
- [ ] tout tourne dans Docker : aucune dépendance ajoutée sur l'hôte ;
- [ ] `README.md` mis à jour (usage, configuration `.env.example`, dépannage) ;
- [ ] [SECURITE.md](SECURITE.md) mis à jour si la surface d'attaque change ;
- [ ] relu (pull request) et fusionné dans `main`.
- [ ] **déployé** avec `make deploy` (version affichée dans Réglages → À propos), et vérifié par l'icône du Bureau.

---

## 6. Backlog produit

Ordonné par valeur et par risque. Priorité MoSCoW : **M** (indispensable),
**S** (important), **C** (souhaitable), **W** (pas maintenant).
Statut : ✅ terminé · 🔄 en cours · ⬜ à faire.

### E1 — Transcription

| ID | Récit utilisateur | Critères d'acceptation | Pts | Prio | Statut |
|---|---|---|---|---|---|
| US-01 | En tant que formateur, je veux transcrire une vidéo en sous-titres horodatés afin de la rendre accessible | `.srt` et `.json` produits ; timestamps absolus corrects sur plusieurs morceaux | 8 | M | ✅ S1 |
| US-02 | En tant qu'exploitant, je veux traiter un fichier de 500 Go sans saturer la mémoire | extraction audio en flux, découpage en morceaux de 30 min, mémoire constante | 8 | M | ✅ S1 |
| US-03 | En tant qu'exploitant, je veux qu'une interruption reprenne là où elle s'est arrêtée | plan `chunks.json` persisté ; morceaux terminés non retranscrits | 5 | M | ✅ S1 |
| US-04 | En tant que formateur, je veux choisir la langue (français / anglais / auto) | langue transmise à Whisper, mémorisée par l'interface | 2 | S | ✅ S2 |
| US-05 | En tant qu'exploitant, je veux la transcription par lots sur GPU | `WHISPER_BATCH_SIZE` ; ≥ 3× plus rapide qu'en séquentiel | 3 | S | ✅ S2 |
| US-06 | En tant qu'utilisateur, je veux identifier les locuteurs | segments annotés `speaker` ; export SRT préfixé | 8 | C | ⬜ |
| US-07 | En tant qu'utilisateur, je veux traduire les sous-titres | `.srt` traduit dans une langue cible | 5 | C | ⬜ |

### E2 — Compression

| ID | Récit utilisateur | Critères d'acceptation | Pts | Prio | Statut |
|---|---|---|---|---|---|
| US-10 | En tant que formateur, je veux réduire la taille d'une vidéo | encodage CRF H.265 ; fichier plus petit que la source | 3 | M | ✅ S1 |
| US-11 | En tant que formateur, je veux des fichiers **sous 300 Mo** pour les déposer sur la plateforme | aucun fichier produit au-dessus de `max_mb` ; ré-essai si l'encodeur dépasse | 8 | M | ✅ S2 |
| US-12 | En tant que formateur, je veux qu'une vidéo trop longue soit découpée en parties | `_compressed_1.mp4`, `_2`… chacune sous la limite, image lisible (≥ 1000 kbps) | 5 | M | ✅ S2 |
| US-13 | En tant que formateur, je veux **le moins de parties possible** | découpage sans ré-encodage selon la taille réelle, coupes équilibrées sur images-clés | 5 | S | ✅ S2 |
| US-14 | En tant qu'exploitant, je veux encoder sur le GPU | NVENC, décodage et mise à l'échelle CUDA, repli CPU automatique | 5 | S | ✅ S2 |
| US-15 | En tant qu'exploitant, je veux traiter un dossier entier de plusieurs To | un job par fichier, arborescence reproduite, doublons en cours ignorés | 5 | S | ✅ S2 |

### E3 — Interface web et API

| ID | Récit utilisateur | Critères d'acceptation | Pts | Prio | Statut |
|---|---|---|---|---|---|
| US-20 | En tant qu'utilisateur, je veux déposer une vidéo par glisser-déposer | upload par morceaux de 64 Mo, 3 tentatives, reprise | 5 | M | ✅ S1 |
| US-21 | En tant qu'utilisateur, je veux suivre la progression | barre rafraîchie toutes les 2,5 s, étape affichée | 3 | M | ✅ S1 |
| US-22 | En tant qu'utilisateur, je veux télécharger toutes les parties d'un coup | `.zip` en flux, dossier au nom de la vidéo | 3 | S | ✅ S2 |
| US-23 | En tant qu'utilisateur Windows, je veux pouvoir extraire le zip | titres ≤ 40 caractères, sans caractère interdit | 2 | S | ✅ S2 |
| US-24 | En tant qu'utilisateur, je veux supprimer une vidéo et tout ce qui en découle | alerte de confirmation ; source, parties, sous-titres et jobs supprimés | 3 | S | ✅ S2 |
| US-25 | En tant qu'utilisateur, je veux gérer les fichiers produits | liste groupée par vidéo, suppression unitaire ou totale | 3 | S | ✅ S2 |
| US-26 | En tant qu'utilisateur, je veux un lecteur synchronisé avec la transcription | clic sur une phrase → position dans la vidéo | 8 | C | ⬜ |

### E4 — Recherche (RAG)

| ID | Récit utilisateur | Critères d'acceptation | Pts | Prio | Statut |
|---|---|---|---|---|---|
| US-30 | En tant qu'apprenant, je veux poser une question et obtenir la minute exacte de la vidéo | passages de ~1200 car. indexés dans Qdrant, timecode dans la réponse | 8 | S | ✅ S2 |
| US-31 | En tant qu'apprenant, je veux retrouver ce qui a été **montré** à l'écran | OCR des diapositives, passages `screen` pour les diapositives muettes | 5 | C | ✅ S2 |
| US-32 | En tant qu'apprenant, je veux une réponse rédigée citant les sources | génération par LLM à partir des passages trouvés | 8 | C | ⬜ |

### E5 — Qualité et tests

| ID | Récit utilisateur | Critères d'acceptation | Pts | Prio | Statut |
|---|---|---|---|---|---|
| US-40 | En tant que développeur, je veux vérifier toute la chaîne en une commande | `scripts/smoke_test.sh` : services, CLI, API, upload | 3 | M | ✅ S1 |
| US-41 | En tant que développeur, je veux une suite de tests automatisés rapide | `make test` dans Docker ; modules, API, sécurité ; < 1 min, sans GPU ni service | 8 | M | 🔄 S2 |
| US-42 | En tant que développeur, je veux que chaque pull request soit testée | GitHub Actions : `make test` + construction des images | 3 | S | ⬜ |
| US-43 | En tant que développeur, je veux mesurer la couverture | `pytest --cov=source`, seuil minimal en CI | 2 | C | ⬜ |

### E6 — Exploitation

| ID | Récit utilisateur | Critères d'acceptation | Pts | Prio | Statut |
|---|---|---|---|---|---|
| US-50 | En tant qu'exploitant, je veux démarrer toute la stack en une commande | `make start` / `make stop`, GPU détecté automatiquement | 2 | M | ✅ S2 |
| US-51 | En tant qu'exploitant, je veux lire les sources sur un disque externe | `MEDIA_PATH`, `OUTPUT_PATH` configurables, traitement sur place | 2 | M | ✅ S2 |
| US-52 | En tant qu'exploitant, je veux stocker les sources sur S3 / MinIO | lecture en flux depuis un bucket | 8 | W | ⬜ |
| US-53 | En tant qu'exploitant, je veux déployer sur plusieurs GPU | manifests Kubernetes, un worker par GPU | 13 | W | ⬜ |
| US-54 | En tant qu'exploitant, je veux une image publiée multi-architecture | `linux/amd64` et `linux/arm64` sur GHCR | 5 | W | ⬜ |

### E7 — Sécurité

Issu de la revue de sécurité du 17/09/2026 — détails dans [SECURITE.md](SECURITE.md).

| ID | Récit utilisateur | Critères d'acceptation | Pts | Prio | Statut |
|---|---|---|---|---|---|
| US-60 | En tant qu'exploitant, je veux une analyse de sécurité documentée | modèle de menaces, protections, vulnérabilités, plan ; tests associés | 5 | M | 🔄 S2 |
| US-61 | En tant qu'exploitant, je veux qu'un nom de fichier envoyé ne puisse pas écrire ailleurs (**VULN-01**) | `test_upload_filename_*` passent ; 400 sur `../` ou chemin absolu | 1 | **M** | ✅ S2 |
| US-62 | En tant qu'exploitant, je veux que l'API et Qdrant ne soient joignables que depuis ma machine (**VULN-03**, **VULN-05**) | ports publiés sur `127.0.0.1` (réglable par `BIND_ADDRESS`) ; `test_ports_bound_to_localhost_by_default` passe | 1 | **M** | ✅ S2 |
| US-63 | En tant qu'exploitant, je veux que les uploads soient bornés (**VULN-02**) | 413 au-delà de la taille déclarée ; espace disque vérifié (507) ; tests SEC-11 | 3 | M | ✅ S2 |
| US-64 | En tant qu'exploitant, je veux protéger l'API par un jeton (**VULN-03**) | 401 sans jeton hors `/`, `/health`, `/login` ; fenêtre de connexion et cookie de session ; tests SEC-10 | 5 | S | ✅ S2 |
| US-65 | En tant qu'exploitant, je veux des secrets non devinables (**VULN-06**) | mot de passe Postgres obligatoire dans `.env` ; tests SEC-13 | 2 | S | ✅ S2 |
| US-69 | En tant qu'exploitant, je veux protéger Qdrant par une clé (défense en profondeur) | `QDRANT__SERVICE__API_KEY`, transmise au client | 1 | C | ⬜ |
| US-66 | En tant qu'exploitant, je veux des conteneurs durcis (**VULN-07**) | image multi-étapes, `no-new-privileges`, `cap_drop`, limites, code en `:ro` | 3 | S | ⬜ |
| US-67 | En tant qu'exploitant, je veux des dépendances maîtrisées (**VULN-09**) | verrouillage avec empreintes, `pip-audit` et `trivy` en CI | 3 | C | ⬜ |
| US-68a | En tant qu'exploitant, je veux une pagination bornée (**VULN-04**) | `1 ≤ limit ≤ 10 000` ; tests SEC-12 | 1 | C | ✅ S2 |
| US-68 | En tant qu'exploitant, je veux réduire les fuites d'information (**VULN-08, 10**) | erreurs génériques, en-têtes HTTP, `TrustedHost` | 2 | C | ⬜ |
| US-70 | En tant qu'exploitant, je veux que les uploads abandonnés ne restent pas sur le disque | purge après 24 h, espace réservé entre uploads simultanés | 2 | C | ⬜ |

---

## 7. Historique des sprints

### Sprint 1 — 31/08/2026 → 11/09/2026 · *Fondations du pipeline*

**Objectif** : transcrire et compresser un fichier volumineux dans Docker, de
bout en bout, avec reprise après interruption.

| Élément | Pts | Résultat |
|---|---|---|
| US-01 Transcription horodatée | 8 | ✅ |
| US-02 Traitement en flux de gros fichiers | 8 | ✅ |
| US-03 Reprise après interruption | 5 | ✅ |
| US-10 Compression CRF | 3 | ✅ |
| US-20 Upload par morceaux | 5 | ✅ |
| US-21 Suivi de progression | 3 | ✅ |
| US-40 Test de bout en bout | 3 | ✅ |
| **Total** | **35 engagés / 35 livrés** | |

Commits : `d42dc7a` (03/09), `fae948f` (03/09), `25d2119` (04/09 — interface web).

**Résultat** : chaîne validée par `scripts/smoke_test.sh` ; transcription mesurée
à 2,3× temps réel sur RTX 3060 (cours de 5 min). Besoin suivant identifié :
des fichiers compressés sous une taille maximale (branche `compression-400mo`)
→ épopée E2 priorisée.

**Rétrospective** (proposée, d'après les constats sur le dépôt)

| Garder | Arrêter | Commencer |
|---|---|---|
| Tout dans Docker, rien sur l'hôte | Messages de commit non descriptifs (« foud it », « no video ») | Messages de commit expliquant le *pourquoi* |
| Test de bout en bout sur clip généré | Versionner `.env` | Tests unitaires rapides à côté du test de bout en bout |

### Sprint 2 — 14/09/2026 → 25/09/2026 · *Compression sous taille garantie* (en cours)

**Objectif** : chaque vidéo, quelle que soit sa durée, produit des fichiers
sous 300 Mo, en un minimum de parties, téléchargeables d'un coup ; le projet
dispose d'une suite de tests et d'une analyse de sécurité.

Branche : `compression-400mo`.

| Élément | Pts | Statut |
|---|---|---|
| US-11 Fichiers sous taille maximale | 8 | ✅ `6a580e3` |
| US-12 Découpage en parties | 5 | ✅ `6a580e3` |
| US-14 Encodage GPU | 5 | ✅ `6a580e3` |
| US-15 Dossiers de plusieurs To | 5 | ✅ `6a580e3` |
| US-50 `make start` / `make stop` | 2 | ✅ `6a580e3` |
| US-51 Sources sur disque externe | 2 | ✅ `6a580e3` |
| US-04 Choix de la langue | 2 | ✅ `6a580e3` |
| US-05 Transcription par lots | 3 | ✅ `6a580e3` |
| US-24 / US-25 Suppression vidéo et sorties | 6 | ✅ `6a580e3` |
| US-30 / US-31 RAG et OCR | 13 | ✅ `6a580e3` |
| US-13 Moins de parties possible | 5 | ✅ `b48c3a4` |
| US-22 / US-23 Zip lisible sous Windows | 5 | ✅ `54c4be6` |
| US-41 Suite de tests automatisés | 8 | 🔄 en revue |
| US-60 Analyse de sécurité | 5 | 🔄 en revue |
| US-61 Nom d'upload assaini (VULN-01) — *ajouté en cours de sprint* | 1 | ✅ |
| US-62 Ports sur `127.0.0.1` (VULN-05) — *ajouté en cours de sprint* | 1 | ✅ |
| US-63 Uploads bornés (VULN-02) — *avancé du sprint 3* | 3 | ✅ |
| US-64 Jeton d'API (VULN-03) — *avancé du sprint 3* | 5 | ✅ |
| US-65 Mot de passe PostgreSQL obligatoire (VULN-06) — *avancé du sprint 3* | 2 | ✅ |
| US-68a Pagination bornée (VULN-04) — *avancé du sprint 3* | 1 | ✅ |
| **Total** | **87 — 74 terminés, 13 en revue** | |

> US-61 et US-62 sont entrées en cours de sprint : faille critique découverte
> par la revue de sécurité, correction d'un point chacune. Le Product Owner a
> ensuite demandé de traiter aussi US-63, 64, 65 et 68a, prévues au sprint 3.
> Exceptions assumées à la protection du sprint, à discuter en rétrospective.

**Suivi (burndown, points restants)**

```
Pts
74 ┤●
   │ ╲
60 ┤  ╲
   │   ●────●                        ← 15/09 : 3 commits, 45 pts livrés
40 ┤         ╲
   │          ╲
20 ┤           ●                     ← 17/09 : tests + sécurité en revue (13 pts)
   │            ╲ ·
 0 ┤─────────────·──·──·──·──·──·─   (idéal en pointillés)
   └┬──┬──┬──┬──┬──┬──┬──┬──┬──┬
    14 15 16 17 18 21 22 23 24 25   septembre
```

> Le sprint 2 a été engagé à 74 points, soit deux fois la vélocité du sprint 1 :
> la charge a pu être tenue car le travail de compression avait commencé
> avant la planification. À corriger en rétrospective (voir §9, R3).

---

## 8. Sprint 3 — proposition

**Du 28/09/2026 au 09/10/2026** — vélocité de référence : ~35 points
(moyenne prudente, le sprint 2 étant atypique).

**Objectif proposé** : *faire vérifier automatiquement chaque changement, et
terminer le durcissement de sécurité.* Les corrections de sécurité prioritaires
(US-61 à 65, 68a) ont été avancées au sprint 2.

| Élément | Pts | Pourquoi maintenant |
|---|---|---|
| US-42 Tests en intégration continue | 3 | empêche toute régression de sécurité |
| US-43 Couverture de code | 2 | |
| US-66 Conteneurs durcis | 3 | |
| US-67 Dépendances verrouillées | 3 | |
| US-68 Fuites d'information (VULN-08, 10) | 2 | |
| US-69 Clé d'API Qdrant | 1 | |
| US-70 Purge des uploads abandonnés | 2 | |
| US-26 Lecteur synchronisé | 8 | valeur utilisateur, si capacité restante |
| **Total** | **24** | ~10 pts de capacité restante, à attribuer par le PO (ex. US-07 traduction, 5 pts) |

---

## 9. Risques et impédiments

| ID | Risque / impédiment | Prob. | Impact | Réponse |
|---|---|---|---|---|
| R1 | Un seul GPU partagé entre développement, tests et traitements réels | Haute | Moyen | tests automatisés sans GPU (`make test`) ; tests GPU planifiés hors traitement |
| R2 | Fuite du jeton d'API partagé (un seul jeton, pas d'identité) | Faible | Élevé | jeton en `.env` non versionné ; rotation invalide les sessions ; exposition réseau seulement derrière TLS |
| R3 | Engagement au-delà de la vélocité (sprint 2 : 74 pts pour 35) | Haute | Moyen | planifier sur la vélocité moyenne, garder 20 % de marge |
| R4 | Pas de Product Owner désigné : priorités fixées par le développeur | Haute | Moyen | désigner un PO parmi les formateurs utilisateurs avant le sprint 3 |
| R5 | Qualité de transcription non mesurée (le test de bout en bout utilise une tonalité) | Moyenne | Moyen | constituer un jeu d'extraits de parole annotés ; mesurer le WER |
| R6 | Mises à jour CUDA / cuDNN cassant CTranslate2 | Faible | Élevé | image épinglée CUDA 12.9 ; test GPU avant toute montée de version |
| R7 | Traitements de plusieurs jours interrompus par une redistribution Celery | Faible | Élevé | `visibility_timeout` de 30 jours, reprise par partie (en place) |

---

## 10. Indicateurs

| Indicateur | Sprint 1 | Sprint 2 | Cible |
|---|---|---|---|
| Vélocité (points terminés) | 35 | 61 (+13 en revue) | stable ± 20 % |
| Engagement tenu | 100 % | 85 % | ≥ 80 % |
| Tests automatisés | 19 vérifications de bout en bout | 21 de bout en bout + 283 tests pytest | croissant |
| Vulnérabilités critiques ouvertes | non évalué | 0 (1 trouvée et corrigée) | 0 |
| Vulnérabilités élevées ouvertes | non évalué | 0 (3 trouvées et corrigées) | 0 |
| Couverture de code (`source/`) | — | 92 % (NVENC non couvert : GPU) | ≥ 90 % |
| Durée de `make test` | — | ~11 s | < 2 min |

Commandes utiles pour la revue :

```bash
git log --since=2026-09-14 --oneline                 # travail livré pendant le sprint
make test                                            # tests unitaires, API, sécurité
./scripts/smoke_test.sh                              # chaîne complète sur la stack démarrée
make test ARGS="-k vuln --runxfail --tb=line"        # état des vulnérabilités connues
```
