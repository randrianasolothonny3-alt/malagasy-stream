import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid

# Sur un clone fraîchement récupéré (autre PC, autre dépôt Git), les paquets
# du projet (Flask, Playwright, ...) ne sont pas encore installés. Plutôt que
# de planter avec un ModuleNotFoundError peu clair dès la première ligne
# d'import, on les installe automatiquement depuis requirements.txt. Doit
# s'exécuter avant tout import tiers (requests/flask/playwright plus bas).
def _paquets_manquants():
    modules = ("flask", "requests", "playwright", "colorama")
    return [module for module in modules if importlib.util.find_spec(module) is None]


def _installer_dependances():
    missing = _paquets_manquants()
    if not missing:
        return
    requirements_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    cible = ["-r", requirements_path] if os.path.isfile(requirements_path) else missing
    print(f"Modules manquants ({', '.join(missing)}), installation en cours...")
    subprocess.run([sys.executable, "-m", "pip", "install", *cible], check=True)
    importlib.invalidate_caches()


_installer_dependances()

# Sur Render (et tout hébergeur où le cache Playwright du build peut ne pas
# se retrouver dans le conteneur d'exécution), on vérifie au démarrage que
# Chromium est bien installé et on l'installe sinon, plutôt que de planter
# à la première extraction avec une erreur demandant de lancer une commande
# dans un terminal auquel on n'a pas accès en prod.
def _chromium_deja_installe(cache_dir):
    if not cache_dir or not os.path.isdir(cache_dir):
        return False
    return any(name.startswith("chromium") for name in os.listdir(cache_dir))


_cache_dir = os.environ.get(
    "PLAYWRIGHT_BROWSERS_PATH",
    os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright"),
)
if not _chromium_deja_installe(_cache_dir):
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
    )

import requests
from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from playwright.sync_api import sync_playwright

from telecharger_episodes import (
    LOW_MEMORY_CHROMIUM_ARGS,
    USER_AGENT,
    FILM_CATEGORIES,
    SERIES_CATEGORIES,
    FrenchStreamScraper,
    download_file,
    fetch_latest_archive,
    probe_resolution_label,
    read_local_video_duration,
)
from anime_sama import AnimeSamaScraper, resolve_episode_download

# En .exe (PyInstaller), les ressources embarquées (templates/) sont extraites
# dans sys._MEIPASS, un dossier temporaire supprimé à la fermeture.
if getattr(sys, "frozen", False):
    RESOURCE_DIR = sys._MEIPASS
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder=os.path.join(RESOURCE_DIR, "templates"))
scraper = FrenchStreamScraper()
anime_sama_scraper = AnimeSamaScraper()


def _is_anime_sama(url):
    return "anime-sama.to" in url


def _scraper_for(url):
    """Choisir automatiquement le scraper selon le domaine, pour que l'UI
    n'ait pas besoin de faire choisir le site à l'utilisateur."""
    return anime_sama_scraper if _is_anime_sama(url) else scraper

# Les fichiers vont dans le vrai dossier "Téléchargements" de l'utilisateur,
# jamais dans le dossier du projet/de l'exe.
DOWNLOADS_ROOT = os.path.join(os.path.expanduser("~"), "Downloads", "Bokolo")

# État des jobs en mémoire: {job_id: {...}}
jobs = {}


def _write_meta(path, data):
    """Sidecar JSON (titre + affiche) écrit à côté des fichiers téléchargés,
    pour que la bibliothèque locale puisse afficher la vraie affiche même
    après un redémarrage du serveur (les jobs en mémoire, eux, sont perdus)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False)
    except OSError:
        pass


def _read_meta(path):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return None


def _download_poster(poster_url, dest_path_no_ext, max_bytes=8 * 1024 * 1024):
    """Télécharger réellement l'affiche sur le disque (pas juste garder son
    URL distante), pour qu'elle reste affichable hors-ligne et même si le
    lien externe (TMDB, GitHub...) devient indisponible plus tard. Renvoie le
    chemin du fichier écrit, ou None si l'affiche n'a pas pu être récupérée
    (dans ce cas la bibliothèque se rabat sur l'URL distante, voir /api/library)."""
    if not poster_url:
        return None
    try:
        with requests.get(poster_url, headers={"User-Agent": USER_AGENT}, timeout=15, stream=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "png" in content_type:
                ext = ".png"
            elif "webp" in content_type:
                ext = ".webp"
            else:
                ext = ".jpg"
            full_dest = dest_path_no_ext + ext
            os.makedirs(os.path.dirname(full_dest), exist_ok=True)
            written = 0
            with open(full_dest, "wb") as file:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError("Affiche trop volumineuse.")
                    file.write(chunk)
        return full_dest
    except Exception:
        return None


@app.route("/api/poster/<path:rel_path>")
def api_poster(rel_path):
    """Servir une affiche téléchargée localement (voir _download_poster)."""
    root = os.path.abspath(DOWNLOADS_ROOT)
    full_path = os.path.abspath(os.path.join(root, rel_path))
    if not full_path.startswith(root + os.sep):
        abort(403)
    if not os.path.isfile(full_path):
        abort(404)
    return send_file(full_path, conditional=True)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    """Espace disque réel du dossier de téléchargement, pour l'affichage
    'Stockage local' de l'interface (pas une valeur inventée)."""
    os.makedirs(DOWNLOADS_ROOT, exist_ok=True)
    usage = shutil.disk_usage(DOWNLOADS_ROOT)
    return jsonify({"free": usage.free, "total": usage.total})


def _poster_url_from_meta(meta):
    """Préfère l'affiche téléchargée en local (fiable, survit si le lien
    distant d'origine meurt); se rabat sur l'URL distante d'origine si le
    fichier local a disparu ou n'a jamais pu être récupéré."""
    poster_file = meta.get("poster_file")
    if poster_file and os.path.isfile(poster_file):
        rel = os.path.relpath(poster_file, DOWNLOADS_ROOT).replace(os.sep, "/")
        return f"/api/poster/{rel}"
    return meta.get("poster")


@app.route("/api/library")
def api_library():
    """Lister ce qui a déjà été téléchargé sur le disque (séries et films),
    avec la vraie affiche quand elle a été enregistrée au moment du
    téléchargement. Rien n'est inventé: un titre sans .mp4 réellement présent
    n'apparaît pas dans la liste."""
    items = []
    if os.path.isdir(DOWNLOADS_ROOT):
        for name in os.listdir(DOWNLOADS_ROOT):
            full = os.path.join(DOWNLOADS_ROOT, name)
            if not os.path.isdir(full):
                continue

            if name == "films":
                for fname in os.listdir(full):
                    if not fname.lower().endswith(".mp4"):
                        continue
                    base = fname[:-4]
                    mp4_path = os.path.join(full, fname)
                    meta = _read_meta(os.path.join(full, base + ".json")) or {}
                    items.append({
                        "type": "film",
                        "key": f"films/{base}",
                        "title": meta.get("title") or base.replace("_", " "),
                        "poster": _poster_url_from_meta(meta),
                        "url": meta.get("url"),
                        "episode_count": 1,
                        "updated_at": os.path.getmtime(mp4_path),
                    })
                continue

            meta = _read_meta(os.path.join(full, "_meta.json")) or {}
            mp4_count = 0
            latest_mtime = os.path.getmtime(full)
            for root, _dirs, files in os.walk(full):
                for fname in files:
                    if fname.lower().endswith(".mp4"):
                        mp4_count += 1
                        latest_mtime = max(latest_mtime, os.path.getmtime(os.path.join(root, fname)))
            if mp4_count == 0:
                continue
            items.append({
                "type": "serie",
                "key": name,
                "title": meta.get("title") or name.replace("_", " "),
                "poster": _poster_url_from_meta(meta),
                "url": meta.get("url"),
                "episode_count": mp4_count,
                "updated_at": latest_mtime,
            })

    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return jsonify(items)


@app.route("/api/library/episodes")
def api_library_episodes():
    """Lister les vrais fichiers déjà présents sur le disque pour une entrée
    du catalogue (fiche détail locale, ne dépend d'aucun lien externe)."""
    item_type = request.args.get("type")
    key = request.args.get("key", "")
    root = os.path.abspath(DOWNLOADS_ROOT)

    if item_type == "film":
        full_path = os.path.abspath(os.path.join(root, key + ".mp4"))
        if not full_path.startswith(root + os.sep) or not os.path.isfile(full_path):
            abort(404)
        rel = os.path.relpath(full_path, root).replace(os.sep, "/")
        return jsonify([{
            "label": "Film",
            "number": None,
            "language": None,
            "stream_url": f"/api/library/stream/{rel}",
            "size": os.path.getsize(full_path),
            "duration_seconds": read_local_video_duration(full_path),
        }])

    base_dir = os.path.abspath(os.path.join(root, key))
    if not base_dir.startswith(root + os.sep) or not os.path.isdir(base_dir):
        abort(404)

    episodes = []
    for lang_name in sorted(os.listdir(base_dir)):
        lang_dir = os.path.join(base_dir, lang_name)
        if not os.path.isdir(lang_dir):
            continue
        for fname in sorted(os.listdir(lang_dir)):
            if not fname.lower().endswith(".mp4"):
                continue
            full_path = os.path.join(lang_dir, fname)
            rel = os.path.relpath(full_path, root).replace(os.sep, "/")
            match = re.search(r"E(\d+)", fname, re.IGNORECASE)
            episodes.append({
                "label": fname,
                "number": int(match.group(1)) if match else None,
                "language": lang_name.upper(),
                "stream_url": f"/api/library/stream/{rel}",
                "size": os.path.getsize(full_path),
                "duration_seconds": read_local_video_duration(full_path),
            })
    episodes.sort(key=lambda ep: (ep["language"] or "", ep["number"] if ep["number"] is not None else -1))
    return jsonify(episodes)


@app.route("/api/library/stream/<path:rel_path>")
def api_library_stream(rel_path):
    """Lire un fichier déjà téléchargé directement depuis la fiche détail du
    catalogue (même mécanisme que /api/stream, mais basé sur le chemin réel
    plutôt que sur un job en mémoire — fonctionne même après un redémarrage)."""
    root = os.path.abspath(DOWNLOADS_ROOT)
    full_path = os.path.abspath(os.path.join(root, rel_path))
    if not full_path.startswith(root + os.sep):
        abort(403)
    if not os.path.isfile(full_path):
        abort(404)
    return send_file(full_path, mimetype="video/mp4", conditional=True)


@app.route("/api/search")
def api_search():
    """Recherche en ligne, par mot-clé, sur les sites intégrés à l'appli
    (french-stream.one, anime-sama.to) via leurs moteurs de recherche
    internes respectifs — pas une recherche dans ce qui est déjà téléchargé."""
    query = (request.args.get("q") or "").strip()
    sites_param = request.args.get("sites") or "french-stream,anime-sama"
    sites = {s.strip() for s in sites_param.split(",") if s.strip()}
    if not query:
        return jsonify({"error": "Mot-clé manquant."}), 400
    if not sites:
        return jsonify({"error": "Sélectionnez au moins un site."}), 400

    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=scraper.headless, args=LOW_MEMORY_CHROMIUM_ARGS)
        try:
            if "french-stream" in sites:
                results.extend(scraper.search(browser, query))
            if "anime-sama" in sites:
                results.extend(anime_sama_scraper.search(browser, query))
        finally:
            browser.close()
    return jsonify(results)


@app.route("/api/latest")
def api_latest():
    """Films/séries de french-stream.one, filtrables par catégorie réelle du
    site, avec pagination. Deux mécanismes selon la catégorie (voir
    FILM_CATEGORIES/SERIES_CATEGORIES): "archive" = vraie page de listing
    paginée du site (total de pages réel), "ajax" = repli sur le bloc
    'Nouveautés' de la home (seule la sous-catégorie s'y filtre correctement,
    mais sans total de pages connu)."""
    content_type = "serie" if request.args.get("type") == "serie" else "film"
    categories = SERIES_CATEGORIES if content_type == "serie" else FILM_CATEGORIES
    by_id = {c["id"]: c for c in categories}
    category_id = request.args.get("category") or categories[0]["id"]
    if category_id not in by_id:
        category_id = categories[0]["id"]
    category = by_id[category_id]
    try:
        page_num = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page_num = 1

    if category["mode"] == "archive":
        items, total_pages, has_prev, has_next = fetch_latest_archive(category["params"], page=page_num)
    else:
        per_page = 18  # aligné sur la taille de page réelle du site
        offset = (page_num - 1) * per_page
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=scraper.headless, args=LOW_MEMORY_CHROMIUM_ARGS)
            try:
                items = scraper.latest(browser, category_id, limit=per_page, offset=offset)
            finally:
                browser.close()
        total_pages = None  # inconnu: ce mécanisme ne donne pas de total
        has_prev = page_num > 1
        has_next = len(items) == per_page

    for item in items:
        item["type"] = content_type

    return jsonify({
        "categories": [{"id": c["id"], "label": c["label"]} for c in categories],
        "active_category": category_id,
        "items": items,
        "page": page_num,
        "total_pages": total_pages,
        "has_prev": has_prev,
        "has_next": has_next,
    })


@app.route("/api/extract", methods=["POST"])
def api_extract():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL manquante."}), 400
    try:
        episodes = _scraper_for(url).extract_episodes(url)
    except Exception as error:
        return jsonify({"error": f"Erreur d'extraction: {error}"}), 500
    if not episodes:
        return jsonify({"error": "Impossible d'extraire les épisodes."}), 500
    return jsonify(episodes)


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.json or {}
    url = data.get("url", "").strip()
    series_title = data.get("series_title", "serie")
    language = data.get("language")
    numbers = data.get("numbers", [])
    quality = data.get("quality") or "auto"
    if quality not in {"auto", "normale", "originale"}:
        quality = "auto"
    poster = data.get("poster")

    if not url or language not in {"vf", "vostfr"} or not numbers:
        return jsonify({"error": "Paramètres invalides."}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "episodes": {
            number: {
                "status": "en_attente", "downloaded": 0, "total": 0, "error": None,
                "quality_label": None, "paused": False,
            }
            for number in numbers
        },
        "done": False,
    }

    if _is_anime_sama(url):
        # Les hébergeurs d'anime-sama.to (sibnet, sendvid) ne donnent qu'un
        # seul lien par épisode: pas de notion de qualité à transmettre ici.
        thread = threading.Thread(
            target=_run_download_job_anime_sama,
            args=(job_id, url, series_title, language, numbers, poster),
            daemon=True,
        )
    else:
        thread = threading.Thread(
            target=_run_download_job,
            args=(job_id, url, series_title, language, numbers, quality, poster),
            daemon=True,
        )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job inconnu."}), 404
    return jsonify(job)


@app.route("/api/file/<job_id>/<int:episode_key>")
def api_file(job_id, episode_key):
    """Servir un fichier téléchargé au navigateur (nécessaire quand l'app
    tourne sur un serveur distant: le fichier atterrit sur le disque du
    serveur, pas sur la machine de l'utilisateur)."""
    job = jobs.get(job_id)
    if not job:
        abort(404)
    state = job["episodes"].get(episode_key) or job["episodes"].get(str(episode_key))
    if not state or state.get("status") != "termine" or not state.get("path"):
        abort(404)

    full_path = os.path.abspath(state["path"])
    if not full_path.startswith(DOWNLOADS_ROOT + os.sep):
        abort(403)
    if not os.path.isfile(full_path):
        abort(404)

    return send_file(full_path, as_attachment=True, download_name=os.path.basename(full_path))


def _episode_state(job_id, episode_key):
    job = jobs.get(job_id)
    if not job:
        return None
    return job["episodes"].get(episode_key) or job["episodes"].get(str(episode_key))


@app.route("/api/pause/<job_id>/<int:episode_key>", methods=["POST"])
def api_pause(job_id, episode_key):
    state = _episode_state(job_id, episode_key)
    if not state:
        return jsonify({"error": "Épisode inconnu."}), 404
    state["paused"] = True
    return jsonify({"ok": True})


@app.route("/api/resume/<job_id>/<int:episode_key>", methods=["POST"])
def api_resume(job_id, episode_key):
    state = _episode_state(job_id, episode_key)
    if not state:
        return jsonify({"error": "Épisode inconnu."}), 404
    state["paused"] = False
    return jsonify({"ok": True})


@app.route("/api/stream/<job_id>/<int:episode_key>")
def api_stream(job_id, episode_key):
    """Servir le fichier déjà téléchargé en lecture directe (sans forcer le
    téléchargement), pour le lecteur vidéo en modal de l'interface."""
    state = _episode_state(job_id, episode_key)
    if not state or state.get("status") != "termine" or not state.get("path"):
        abort(404)

    full_path = os.path.abspath(state["path"])
    if not full_path.startswith(DOWNLOADS_ROOT + os.sep):
        abort(403)
    if not os.path.isfile(full_path):
        abort(404)

    return send_file(full_path, mimetype="video/mp4", conditional=True)


@app.route("/api/watch/<job_id>/<int:episode_key>")
def api_watch(job_id, episode_key):
    """Lire la vidéo en direct depuis l'hébergeur distant, dès que son lien a
    été résolu — sans attendre la fin du téléchargement local. Le navigateur
    ne peut pas lui-même envoyer le bon Referer vers un <video src> cross-site
    (anti-hotlink de l'hébergeur), donc on relaie la requête (avec Range pour
    permettre de chercher dans la vidéo) depuis le serveur."""
    state = _episode_state(job_id, episode_key)
    remote_url = state.get("remote_url") if state else None
    if not remote_url:
        abort(404)

    headers = {"User-Agent": USER_AGENT, "Referer": state.get("remote_referer") or remote_url}
    range_header = request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header

    try:
        upstream = requests.get(remote_url, headers=headers, stream=True, timeout=30)
    except requests.RequestException:
        abort(502)
    if upstream.status_code not in (200, 206):
        upstream.close()
        abort(502)

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response_headers = {
        key: upstream.headers[key]
        for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
        if key in upstream.headers
    }
    response_headers.setdefault("Content-Type", "video/mp4")
    response_headers.setdefault("Accept-Ranges", "bytes")
    return Response(generate(), status=upstream.status_code, headers=response_headers)


@app.route("/api/extract_film", methods=["POST"])
def api_extract_film():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL manquante."}), 400
    try:
        info = scraper.get_movie_options(url)
    except Exception as error:
        return jsonify({"error": f"Erreur d'extraction: {error}"}), 500
    if not info["options"]:
        return jsonify({"error": "Aucun lien de téléchargement trouvé pour ce film."}), 500
    return jsonify(info)


@app.route("/api/download_film", methods=["POST"])
def api_download_film():
    data = request.json or {}
    title = data.get("title", "film")
    data_url = data.get("data_url", "").strip()
    poster = data.get("poster")
    source_url = data.get("url")
    if not data_url:
        return jsonify({"error": "Paramètres invalides."}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "episodes": {0: {
            "status": "en_attente", "downloaded": 0, "total": 0, "error": None,
            "quality_label": None, "paused": False,
        }},
        "done": False,
    }

    thread = threading.Thread(
        target=_run_movie_download_job,
        args=(job_id, title, data_url, poster, source_url),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


def _run_movie_download_job(job_id, title, data_url, poster=None, source_url=None):
    job = jobs[job_id]
    state = job["episodes"][0]
    safe_title = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_") or "film"
    poster_file = _download_poster(poster, os.path.join(DOWNLOADS_ROOT, "films", f"{safe_title}_affiche"))
    _write_meta(
        os.path.join(DOWNLOADS_ROOT, "films", f"{safe_title}.json"),
        {"title": title, "poster": poster, "poster_file": poster_file, "url": source_url, "type": "film"},
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=scraper.headless, args=LOW_MEMORY_CHROMIUM_ARGS)
        try:
            state["status"] = "resolution"
            result = scraper.resolve_movie_download(browser, data_url)
            if not result:
                state["status"] = "erreur"
                state["error"] = "Lien de téléchargement introuvable."
                return

            state["status"] = "telechargement"
            state["quality_label"] = probe_resolution_label(result["url"], referer=result.get("referer"))
            state["remote_url"] = result["url"]
            state["remote_referer"] = result.get("referer")
            state["watch_url"] = f"/api/watch/{job_id}/0"
            dest_path = os.path.join(DOWNLOADS_ROOT, "films", f"{safe_title}.mp4")
            state["path"] = dest_path

            def on_progress(downloaded, total):
                state["downloaded"] = downloaded
                state["total"] = total

            try:
                download_file(result["url"], dest_path, referer=result["referer"], on_progress=on_progress, control=state)
                state["status"] = "termine"
            except Exception as error:
                state["status"] = "erreur"
                state["error"] = str(error)
        finally:
            browser.close()
            job["done"] = True


def _run_download_job_anime_sama(job_id, series_url, series_title, language, numbers, poster=None):
    """Équivalent de _run_download_job pour anime-sama.to: pas de Playwright
    nécessaire, on ré-extrait la liste d'épisodes (avec ses hébergeurs) via
    de simples requêtes HTTP, puis on résout/télécharge chaque épisode."""
    job = jobs[job_id]
    safe_title = re.sub(r"[^\w\s-]", "", series_title).strip().replace(" ", "_") or "serie"
    output_dir = os.path.join(DOWNLOADS_ROOT, safe_title, language)
    poster_file = _download_poster(poster, os.path.join(DOWNLOADS_ROOT, safe_title, "affiche"))
    _write_meta(
        os.path.join(DOWNLOADS_ROOT, safe_title, "_meta.json"),
        {"title": series_title, "poster": poster, "poster_file": poster_file, "url": series_url, "type": "serie"},
    )

    try:
        episodes = anime_sama_scraper.extract_episodes(series_url)
    except Exception as error:
        for number in numbers:
            state = job["episodes"][number]
            state["status"] = "erreur"
            state["error"] = f"Extraction impossible: {error}"
        job["done"] = True
        return

    episodes_by_number = {episode["number"]: episode for episode in episodes.get(language, [])}

    for number in numbers:
        state = job["episodes"][number]
        state["status"] = "resolution"

        episode = episodes_by_number.get(number)
        result = resolve_episode_download(episode["hosts"]) if episode else None

        if not result:
            state["status"] = "erreur"
            state["error"] = "Lien de téléchargement introuvable (hébergeurs non supportés ou HLS uniquement)."
            continue

        state["status"] = "telechargement"
        state["quality_label"] = probe_resolution_label(result["url"], referer=result.get("referer"))
        state["remote_url"] = result["url"]
        state["remote_referer"] = result.get("referer")
        state["watch_url"] = f"/api/watch/{job_id}/{number}"
        filename = f"{safe_title}_{language.upper()}_E{number:02d}.mp4"
        dest_path = os.path.join(output_dir, filename)
        state["path"] = dest_path

        def on_progress(downloaded, total, state=state):
            state["downloaded"] = downloaded
            state["total"] = total

        try:
            download_file(result["url"], dest_path, referer=result["referer"], on_progress=on_progress, control=state)
            state["status"] = "termine"
        except Exception as error:
            state["status"] = "erreur"
            state["error"] = str(error)

    job["done"] = True


def _run_download_job(job_id, series_url, series_title, language, numbers, quality="auto", poster=None):
    job = jobs[job_id]
    safe_title = re.sub(r"[^\w\s-]", "", series_title).strip().replace(" ", "_") or "serie"
    output_dir = os.path.join(DOWNLOADS_ROOT, safe_title, language)
    poster_file = _download_poster(poster, os.path.join(DOWNLOADS_ROOT, safe_title, "affiche"))
    _write_meta(
        os.path.join(DOWNLOADS_ROOT, safe_title, "_meta.json"),
        {"title": series_title, "poster": poster, "poster_file": poster_file, "url": series_url, "type": "serie"},
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=scraper.headless, args=LOW_MEMORY_CHROMIUM_ARGS)
        page = browser.new_page(user_agent=USER_AGENT)
        # Ces sites ouvrent des popups publicitaires au moindre clic; on les
        # referme aussitôt pour qu'elles ne fassent pas planter les clics suivants.
        page.on("popup", lambda popup: popup.close())
        try:
            page.goto(series_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector(f"#{language}-episodes .episode-row", timeout=20000)

            for number in numbers:
                state = job["episodes"][number]
                state["status"] = "resolution"

                result = None
                for _ in range(3):
                    result = scraper.get_direct_download_url(page, language, number, quality=quality)
                    if result:
                        break
                    page.wait_for_timeout(1500)

                if not result:
                    state["status"] = "erreur"
                    state["error"] = "Lien de téléchargement introuvable."
                    continue

                state["status"] = "telechargement"
                state["quality_label"] = probe_resolution_label(result["url"], referer=result.get("referer"))
                state["remote_url"] = result["url"]
                state["remote_referer"] = result.get("referer")
                state["watch_url"] = f"/api/watch/{job_id}/{number}"
                filename = f"{safe_title}_{language.upper()}_E{number:02d}.mp4"
                dest_path = os.path.join(output_dir, filename)
                state["path"] = dest_path

                def on_progress(downloaded, total, state=state):
                    state["downloaded"] = downloaded
                    state["total"] = total

                try:
                    download_file(result["url"], dest_path, referer=result["referer"], on_progress=on_progress, control=state)
                    state["status"] = "termine"
                except Exception as error:
                    state["status"] = "erreur"
                    state["error"] = str(error)
        finally:
            browser.close()
            job["done"] = True


if __name__ == "__main__":
    app.run(debug=True, port=5000)
