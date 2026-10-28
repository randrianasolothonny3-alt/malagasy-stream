import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

if sys.platform == "win32":
    try:
        import colorama
        colorama.just_fix_windows_console()
    except ImportError:
        pass


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    BLUE = "\033[34m"


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Réduit l'empreinte mémoire de Chromium, important sur les hébergeurs à RAM
# limitée (ex: plan free de Render, 512 Mo) où Chromium peut sinon être tué
# par le système en pleine extraction (OOM) et provoquer un 502 côté client.
LOW_MEMORY_CHROMIUM_ARGS = [
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--js-flags=--max-old-space-size=128",
]

# L'hébergeur vidzy propose parfois deux liens de téléchargement distincts sur
# sa page "/d/": un en qualité réduite (_n) et un en qualité d'origine (_o).
# Ce ne sont pas de vraies résolutions (1080p/720p/...): on ne montre donc que
# les niveaux qui existent réellement pour la vidéo en cours, jamais une liste
# fixe inventée.
QUALITY_SUFFIXES = {"normale": "_n", "originale": "_o"}
QUALITY_LABELS = {"normale": "Qualité normale", "originale": "Qualité originale"}


# ---------------------------------------------------------------------------
# Détection de la vraie résolution vidéo (1080p/720p/.../4K), sans ffmpeg.
# On ne lit que l'en-tête du MP4 distant (quelques centaines de Ko à ~1,5 Mo,
# via des requêtes HTTP Range), jamais le fichier entier, et on parse la boîte
# "stsd" du conteneur MP4 pour lire la largeur/hauteur réelles de la piste
# vidéo. Ça évite une dépendance ffmpeg (binaire natif, lourd à déployer sur
# un plan Render gratuit déjà limité en RAM) tout en donnant un vrai résultat
# mesuré plutôt qu'un libellé générique ou une résolution inventée.
# ---------------------------------------------------------------------------

def _iter_mp4_boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        box_type = data[pos + 4:pos + 8]
        header_size = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header_size = 16
        elif size == 0:
            size = end - pos
        if size < header_size or pos + size > end:
            break
        yield box_type, pos + header_size, pos + size
        pos += size


def _find_mp4_box(data, path, start=0, end=None):
    target, rest = path[0], path[1:]
    for box_type, box_start, box_end in _iter_mp4_boxes(data, start, end):
        if box_type == target:
            if not rest:
                return box_start, box_end
            return _find_mp4_box(data, rest, box_start, box_end)
    return None


def _locate_box_by_signature(data, box_type):
    """Repli pour un buffer qui ne commence pas forcément sur une frontière de
    boîte (ex: derniers Mo d'un fichier, lus à un décalage arbitraire): on
    cherche directement la signature du type de boîte dans les octets, puis on
    relit sa taille déclarée juste avant (format MP4: [taille:4][type:4])."""
    idx = data.find(box_type)
    if idx < 4:
        return None
    box_start = idx - 4
    size = struct.unpack(">I", data[box_start:box_start + 4])[0]
    header_size = 8
    if size == 1:
        if box_start + 16 > len(data):
            return None
        size = struct.unpack(">Q", data[box_start + 8:box_start + 16])[0]
        header_size = 16
    box_end = len(data) if size <= 0 else min(box_start + size, len(data))
    payload_start = box_start + header_size
    if payload_start > box_end:
        return None
    return payload_start, box_end


def _find_moov_box(data):
    """'moov' structurellement (buffer aligné sur le vrai début du fichier,
    cas 'faststart'), sinon par recherche de signature (buffer arbitraire,
    cas le plus courant où 'moov' est écrite après les données vidéo)."""
    return _find_mp4_box(data, [b"moov"]) or _locate_box_by_signature(data, b"moov")


def _video_dims_from_stsd(data, stsd_start, stsd_end):
    """Boîte 'stsd': version(1)+flags(3)+entry_count(4), puis une ou plusieurs
    'sample entries' (ex: 'avc1'/'hev1') dont les 4 derniers octets d'un bloc
    de 28 représentent largeur/hauteur (format VisualSampleEntry, ISO 14496-12)."""
    pos = stsd_start + 8
    for _entry_type, entry_start, entry_end in _iter_mp4_boxes(data, pos, stsd_end):
        if entry_start + 28 <= entry_end:
            width, height = struct.unpack(">HH", data[entry_start + 24:entry_start + 28])
            if width and height:
                return width, height
    return None


def _probe_mp4_dimensions(data):
    moov = _find_moov_box(data)
    if not moov:
        return None
    moov_start, moov_end = moov
    for box_type, trak_start, trak_end in _iter_mp4_boxes(data, moov_start, moov_end):
        if box_type != b"trak":
            continue
        mdia = _find_mp4_box(data, [b"mdia"], trak_start, trak_end)
        if not mdia:
            continue
        mdia_start, mdia_end = mdia
        hdlr = _find_mp4_box(data, [b"hdlr"], mdia_start, mdia_end)
        if not hdlr:
            continue
        hdlr_start, _hdlr_end = hdlr
        # hdlr: version(1)+flags(3)+pre_defined(4)+handler_type(4) -> "vide" pour une piste vidéo.
        if data[hdlr_start + 8:hdlr_start + 12] != b"vide":
            continue
        stsd = _find_mp4_box(data, [b"minf", b"stbl", b"stsd"], mdia_start, mdia_end)
        if not stsd:
            continue
        dims = _video_dims_from_stsd(data, *stsd)
        if dims:
            return dims
    return None


def _fetch_byte_range(url, referer, start, length):
    """Lire au plus `length` octets à partir de `start`, même si le serveur
    ignore l'en-tête Range (le plafond est appliqué côté client via
    raw.read(length)): on ne risque jamais de rapatrier le fichier entier."""
    headers = {"User-Agent": USER_AGENT, "Range": f"bytes={start}-{start + length - 1}"}
    if referer:
        headers["Referer"] = referer
    with requests.get(url, headers=headers, timeout=20, stream=True) as response:
        data = response.raw.read(length, decode_content=True)
        total = None
        if response.status_code == 206:
            content_range = response.headers.get("Content-Range", "")
            if "/" in content_range:
                try:
                    total = int(content_range.rsplit("/", 1)[-1])
                except ValueError:
                    total = None
        else:
            try:
                total = int(response.headers.get("Content-Length", 0)) or None
            except (TypeError, ValueError):
                total = None
        return data, total


def _label_for_height(height):
    """Classement par hauteur réelle mesurée du flux vidéo (pas une estimation)."""
    if height >= 1900:
        return "4K UHD"
    if height >= 980:
        return "1080p Full HD"
    if height >= 680:
        return "720p HD"
    if height >= 440:
        return "480p SD"
    if height >= 300:
        return "360p"
    return f"{height}p"


def probe_resolution_label(url, referer=None, chunk=1_500_000):
    """Déterminer la vraie résolution d'une vidéo MP4 distante en ne lisant
    que son en-tête: on essaie d'abord le début du fichier (cas 'faststart',
    boîte 'moov' avant les données), sinon sa fin (cas le plus courant, 'moov'
    écrite après les données). Renvoie None si la détection échoue, plutôt
    que d'inventer une résolution."""
    try:
        head_data, total = _fetch_byte_range(url, referer, 0, chunk)
        dims = _probe_mp4_dimensions(head_data)
        if not dims and total and total > len(head_data):
            tail_data, _ = _fetch_byte_range(url, referer, max(0, total - chunk), chunk)
            dims = _probe_mp4_dimensions(tail_data)
        if not dims:
            return None
        _width, height = dims
        return _label_for_height(height)
    except Exception:
        return None


def read_local_video_duration(file_path, chunk=2_000_000):
    """Lire la vraie durée (en secondes) d'un fichier MP4 déjà présent sur le
    disque, en parsant la boîte 'mvhd' de son en-tête (début ou fin du
    fichier selon que 'moov' est en tête ou en queue). Pas de dépendance
    ffmpeg: on ne lit que quelques Mo, jamais le fichier entier. Renvoie None
    si la durée n'a pas pu être lue, plutôt que d'inventer une valeur."""
    try:
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as file:
            data = file.read(min(chunk, size))
            moov = _find_moov_box(data)
            if not moov and size > len(data):
                file.seek(max(0, size - chunk))
                data = file.read(chunk)
                moov = _find_moov_box(data)
            if not moov:
                return None
            moov_start, moov_end = moov
            mvhd = _find_mp4_box(data, [b"mvhd"], moov_start, moov_end)
            if not mvhd:
                return None
            start, _end = mvhd
            version = data[start]
            if version == 1:
                timescale = struct.unpack(">I", data[start + 20:start + 24])[0]
                duration = struct.unpack(">Q", data[start + 24:start + 32])[0]
            else:
                timescale = struct.unpack(">I", data[start + 12:start + 16])[0]
                duration = struct.unpack(">I", data[start + 16:start + 20])[0]
            if not timescale:
                return None
            return duration / timescale
    except Exception:
        return None


FS_SEARCH_ITEM_RE = re.compile(
    r"<div class='search-item'[^>]*onclick=\"location\.href='([^']+)'\"[^>]*>"
    r".*?<img src='([^']*)'[^>]*alt='([^']*)'"
    r".*?<div class='search-title'>([^<]*)</div>",
    re.DOTALL,
)

# Catégories réelles des blocs "Nouveautés" de la page d'accueil du site
# (identifiants de catégorie DLE lus dans son propre HTML, pas inventés).
# "archive": page de listing réelle et paginée du site (/index.php?do=...),
# accessible en simple requête HTTP (pas bloquée comme /engine/ajax/*) et qui
# expose son vrai nombre total de pages — pas une estimation.
# "ajax": repli sur le bloc 'Nouveautés' de la home (voir latest()); le site
# ne filtre correctement par cette sous-catégorie que via ce module AJAX, qui
# lui ne donne aucun total de pages (pagination suivant/précédent seulement).
FILM_CATEGORIES = [
    {"id": "all", "label": "Tous", "mode": "archive", "params": {"do": "cat", "category": "films"}},
    {"id": "animation", "label": "Animation", "mode": "archive", "params": {"do": "xfsearch", "xfname": "genre-1", "xf": "Animation"}},
    {"id": "comedie", "label": "Comédie", "mode": "archive", "params": {"do": "xfsearch", "xfname": "genre-1", "xf": "Comédie"}},
    {"id": "action", "label": "Action", "mode": "archive", "params": {"do": "xfsearch", "xfname": "genre-1", "xf": "Action"}},
    {"id": "aventure", "label": "Aventure", "mode": "archive", "params": {"do": "xfsearch", "xfname": "genre-1", "xf": "Aventure"}},
    {"id": "drame", "label": "Drame", "mode": "archive", "params": {"do": "xfsearch", "xfname": "genre-1", "xf": "Drame"}},
    {"id": "horreur", "label": "Horreur", "mode": "archive", "params": {"do": "xfsearch", "xfname": "genre-1", "xf": "Horreur"}},
]
SERIES_CATEGORIES = [
    {"id": "all", "label": "Tous", "mode": "archive", "params": {"do": "cat", "category": "s-tv"}},
    {"id": "vf", "label": "VF", "mode": "archive", "params": {"do": "xfsearch", "xfname": "version-serie", "xf": "VF"}},
    {"id": "vostfr", "label": "VOSTFR", "mode": "archive", "params": {"do": "xfsearch", "xfname": "version-serie", "xf": "VOSTFR"}},
    {"id": "24", "label": "Netflix", "mode": "ajax"},
    {"id": "82", "label": "Apple TV", "mode": "ajax"},
    {"id": "83", "label": "Prime Video", "mode": "ajax"},
    {"id": "84", "label": "Disney+", "mode": "ajax"},
    {"id": "71", "label": "K-Drama", "mode": "ajax"},
]

# Catégories réelles du menu de pornhub.com (identifiants "c=" lus dans son
# propre HTML, pas inventés): un sous-ensemble des plus utilisées, comme pour
# FILM_CATEGORIES/SERIES_CATEGORIES ci-dessus.
PORNO_CATEGORIES = [
    {"id": "all", "label": "Tous"},
    {"id": "29", "label": "MILF"},
    {"id": "28", "label": "Mature"},
    {"id": "27", "label": "Lesbienne"},
    {"id": "35", "label": "Sodomie"},
    {"id": "65", "label": "Ménage à trois"},
    {"id": "80", "label": "Gangbang"},
    {"id": "111", "label": "Japonais"},
    {"id": "17", "label": "Black"},
    {"id": "8", "label": "Gros seins"},
    {"id": "24", "label": "Publique"},
    {"id": "10", "label": "Bondage"},
    {"id": "22", "label": "Masturbation"},
    {"id": "86", "label": "Animé"},
    {"id": "kw:enceinte", "label": "Femme enceinte"},
]

PORNO_SORT_OPTIONS = [
    {"id": "", "label": "Pertinence"},
    {"id": "mr", "label": "Plus récentes"},
    {"id": "mv", "label": "Plus vues"},
    {"id": "tr", "label": "Mieux notées"},
    {"id": "lg", "label": "Plus longues"},
]
PORNO_SORT_VALUES = {opt["id"] for opt in PORNO_SORT_OPTIONS if opt["id"]}

PORNO_DURATION_OPTIONS = [
    {"id": "", "label": "Toutes durées", "min": None, "max": None},
    {"id": "short", "label": "Courtes (< 10 min)", "min": None, "max": 10},
    {"id": "medium", "label": "Moyennes (10-20 min)", "min": 10, "max": 20},
    {"id": "long", "label": "Longues (> 20 min)", "min": 20, "max": None},
]

# ---------------------------------------------------------------------------
# FOOTBALL EN DIRECT (rbtvplus18.surf / fctv33go.lol, plateforme "digit")
#
# SPA Nuxt servie sur une vingtaine de domaines miroirs: les routes sont
# identiques partout (seul l'hôte change), donc tous les liens sont normalisés
# sur le domaine choisi. La page /football.html arrive VIDE dans le HTML brut
# (simple squelette) et se remplit en JS -> navigateur obligatoire.
#
# Le lecteur n'est pas chez un hébergeur externe: la page charge une iframe
# interne /player.html et publie le vrai flux (HLS .m3u8) dans le store Vue
# ($store.state.player.cStreamItem.mil). On lit ce store, donc on récupère une
# URL exploitable telle quelle par download_hls() / record_live_hls() / VLC.
# ---------------------------------------------------------------------------

FOOTBALL_BASE = "https://www.rbtvplus18.surf"
FOOTBALL_LOCALE = "/fr"                    # "" pour l'anglais
FOOTBALL_SPORT_PATH = "/football.html"     # "/live.html" = tous les sports
FOOTBALL_SPORT_PATHS = ("/football.html", "/live.html")
FOOTBALL_DOMAINS = ("rbtvplus", "fctv33", "rbtv77", "rbsports77",
                    "superabbit77", "madplay77")

# Entrée de sidebar (lue par l'interface web, si elle en a un).
FOOTBALL_SIDEBAR_ENTRY = {
    "id": "football",
    "label": "Football en direct",
    "icon": "icon_st_ftb",          # icône sport déjà servie par le site
    "type": "live",
    "domains": FOOTBALL_DOMAINS,
    "url": FOOTBALL_BASE + FOOTBALL_LOCALE + FOOTBALL_SPORT_PATH,
}

# Arguments Chromium communs au football: on part des arguments "basse
# mémoire" et on ajoute la politique d'autoplay. Sans elle, Chromium bloque la
# lecture automatique et le <video> ne réclame JAMAIS son .m3u8 -> le store
# Vuex reste vide et aucun flux n'est récupérable.
CHROMIUM_ARGS = LOW_MEMORY_CHROMIUM_ARGS + [
    "--autoplay-policy=no-user-gesture-required",
]

# Extraction STRUCTURELLE des lignes de match depuis le DOM rendu.
#
# Ne dépend ni des classes CSS (hashs qui changent à chaque build) ni du
# motif d'URL (le lien cliquable est parfois une icône TV avec @click, pas un
# <a href>): une ligne de match est reconnue par sa STRUCTURE, une heure
# HH:MM et un score "a - b" portés par deux feuilles du même conteneur le
# plus fin qui les contient (évite les doublons parent/enfant). Le marqueur
# "en direct" est le badge minute (44', 65', 90+'), pas le bloc de score: sur
# l'onglet Live du site, le score est affiché pour tout le monde, le badge
# minute non.
FOOTBALL_MATCHES_JS = r"""
() => {
    const SCORE = /^\d{1,2}\s*[-–:]\s*\d{1,2}$/;
    const TIME  = /^\d{1,2}:\d{2}$/;
    const MIN   = /^\d{1,3}(?:\s*\+\s*\d+)?\s*['’′]/;
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const leavesOf = el => Array.from(el.querySelectorAll('*')).filter(n => !n.children.length);

    const hasTimeScore = el => {
        let t = false, s = false;
        for (const n of leavesOf(el)) {
            const v = clean(n.textContent);
            if (!v) continue;
            if (!t && TIME.test(v)) t = true;
            else if (!s && SCORE.test(v)) s = true;
            if (t && s) return true;
        }
        return false;
    };

    const cands = [];
    for (const el of document.querySelectorAll('div, li, tr, article, section, a')) {
        const t = clean(el.textContent);
        if (!t || t.length > 240) continue;
        if (el.children.length < 2) continue;
        if (!hasTimeScore(el)) continue;
        if (Array.from(el.children).some(c => hasTimeScore(c))) continue;
        cands.push(el);
    }

    cands.forEach((el, i) => el.setAttribute('data-fb-idx', String(i)));

    return cands.map((el, i) => {
        const texts = leavesOf(el).map(n => clean(n.textContent)).filter(Boolean);

        let minute = null, score = null, home = null, away = null, time = null;
        for (let k = 0; k < texts.length; k++) {
            if (!time && TIME.test(texts[k])) time = texts[k];
            if (!minute && MIN.test(texts[k])) minute = texts[k];
            if (!score && SCORE.test(texts[k])) {
                score = texts[k];
                home = texts[k - 1] || null;
                away = texts[k + 1] || null;
            }
        }

        // championnat: ligne d'en-tete juste au-dessus (texte court, sans heure)
        let league = null, sib = el.previousElementSibling;
        for (let k = 0; k < 3 && sib; k++) {
            const t = clean(sib.textContent);
            if (t && t.length < 70 && !TIME.test(t) && !SCORE.test(t) && !/1\s*X\s*2/.test(t)) {
                league = t; break;
            }
            sib = sib.previousElementSibling;
        }

        const links = Array.from(el.querySelectorAll('a[href]')).map(a => a.href).slice(0, 8);
        const direct = el.matches('a[href]') ? el.href : null;
        const matchHref = links.find(h => /-match-|\/match\//i.test(h)) || direct || null;

        return {
            idx: i,
            text: clean(el.textContent),
            home, away, minute, score, league, time,
            links, href: matchHref,
            live: !!minute,
            html: el.outerHTML.slice(0, 500),
        };
    });
}
"""

# Clic sur la ligne N (l'icône TV/lecteur d'abord, la ligne en repli): utilisé
# par get_stream() quand aucun <a href> exploitable n'a été trouvé sur la ligne.
FOOTBALL_CLICK_JS = r"""
(index) => {
    const el = document.querySelector('[data-fb-idx="' + index + '"]');
    if (!el) return false;
    const icon = el.querySelector('i[class*=tv], svg[class*=tv], img[class*=tv], ' +
                                  '[class*=player], [class*=play], [class*=live]');
    const target = icon || el.querySelector('a[href]') || el;
    target.scrollIntoView({block: 'center'});
    target.click();
    return true;
}
"""

# Lecture de l'état du lecteur. Évalué sur TOUTES les frames (page parente ET
# iframe /player.html): la première qui expose un "mil" exploitable gagne.
# On couvre plusieurs formes de store (Nuxt 2 window.$nuxt, Nuxt 3
# __vue_app__, payload __NUXT__) et, en dernier recours, on cherche une clé
# "mil" en profondeur dans l'état, car un Nuxt 3 peut aplatir le store.
FOOTBALL_STORE_JS = r"""
() => {
    const states = [];
    try { if (window.$nuxt && window.$nuxt.$store) states.push(window.$nuxt.$store.state); } catch (e) {}
    try {
        const el = document.getElementById('__nuxt');
        const app = el && el.__vue_app__;
        if (app) states.push((app.config.globalProperties.$store || {}).state || {});
    } catch (e) {}
    try { if (window.__NUXT__) states.push(window.__NUXT__); } catch (e) {}

    let mil = null, name = null, servers = [];
    for (const s of states) {
        const p = (s && s.player) || {};
        const item = p.cStreamItem || {};
        if (item.mil) { mil = item.mil; name = item.name || name; }
        if (Array.isArray(p.cStreamList) && p.cStreamList.length) {
            servers = p.cStreamList.map(x => ({name: x.name, streamId: x.streamId, siteType: x.siteType}));
        }
        if (!mil) {   // Nuxt 3: etat aplati -> recherche profonde d'une cle "mil"
            const stack = [s];
            let guard = 0;
            while (stack.length && guard++ < 5000) {
                const cur = stack.pop();
                if (!cur || typeof cur !== 'object') continue;
                if (typeof cur.mil === 'string' && cur.mil) { mil = cur.mil; break; }
                for (const k in cur) { if (cur[k] && typeof cur[k] === 'object') stack.push(cur[k]); }
            }
        }
    }
    const v = document.querySelector('video');
    return { mil, name, servers, videoSrc: (v && v.src && !v.src.startsWith('blob:')) ? v.src : null };
}
"""


def _football_dump(page, path="football_debug.html"):
    """Écrire le DOM rendu sur disque pour inspecter ce que le site renvoie
    vraiment (redirection, anti-bot, gabarit différent...)."""
    try:
        with open(path, "w", encoding="utf-8") as file:
            file.write(page.content())
    except Exception:
        pass
    print(f"  {Colors.DIM}[debug] DOM -> {path} | URL={page.url} | "
          f"titre={page.title()!r} | frames={len(page.frames)}{Colors.RESET}")


def football_debug(url=f"{FOOTBALL_BASE}{FOOTBALL_LOCALE}{FOOTBALL_SPORT_PATH}"):
    """Diagnostic à lancer UNE fois quand la liste de matchs revient vide.

    Affiche ce que la page rend réellement: URL finale (une différence avec
    l'URL demandée = redirection), titre (un titre de type "Just a moment" =
    page anti-bot), nombre de liens, motifs d'URL trouvés, flux .m3u8 vus."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        page = browser.new_page(user_agent=USER_AGENT, locale="fr-FR",
                                viewport={"width": 1440, "height": 900})
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        seen = []
        page.on("response", lambda response: seen.append(response.url))
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        for _ in range(4):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)

        print("URL demandee  :", url)
        print("URL finale    :", page.url)
        print("Titre         :", page.title())
        print("Frames        :", [frame.url for frame in page.frames])
        hrefs = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.getAttribute('href'))"
        )
        print("Nb <a[href]>  :", len(hrefs))
        print("a[-match-]    :", len([h for h in hrefs if h and "-match-" in h]))
        print("a[/football/] :", len([h for h in hrefs if h and "/football/" in h]))
        print("Hrefs foot    :", [h for h in hrefs if h and "football" in h][:15])
        print("Hrefs (30)    :", hrefs[:30])
        print("m3u8 vus      :", [u for u in seen if ".m3u8" in u][:5])
        _football_dump(page, "football_debug.html")
        browser.close()


LATEST_ITEM_RE = re.compile(
    r'<a class="short-poster[^"]*"[^>]*href="([^"]+)"[^>]*>.*?<img src="([^"]*)"'
    r'.*?<div class="short-title">([^<]*)</div>',
    re.DOTALL,
)
VERSION_RE = re.compile(r'<span class="film-version"><a[^>]*>([^<]*)</a></span>')


def _parse_short_items(html):
    """Analyser les fiches 'short' (gabarit commun du site pour les listings
    archive ET le bloc AJAX 'Nouveautés'): titre, affiche, lien, et la version
    (VF/VOSTFR/...) quand le site l'affiche sur la vignette elle-même — évite
    d'avoir à ouvrir chaque fiche individuellement pour cette seule info.
    Chaque fiche est entourée de <div class="short">...</div>: en scopant la
    recherche à ce bloc, la version lue est bien celle de LA bonne fiche (pas
    celle d'une fiche voisine, comme ce serait le cas avec un simple findall
    global sur toute la page)."""
    chunks = html.split('<div class="short">')
    if len(chunks) > 1:
        results = []
        for chunk in chunks[1:]:
            match = LATEST_ITEM_RE.search(chunk)
            if not match:
                continue
            href, poster, title = match.groups()
            version_match = VERSION_RE.search(chunk)
            results.append({
                "title": unescape(title).strip(),
                "poster": poster or None,
                "href": href,
                "version": version_match.group(1).strip() if version_match else None,
            })
        return results
    # Repli: gabarit sans le wrapper <div class="short"> (ex: autre bloc
    # AJAX) — on retrouve au moins les fiches, mais pas leur version.
    return [
        {"title": unescape(title).strip(), "poster": poster or None, "href": href, "version": None}
        for href, poster, title in LATEST_ITEM_RE.findall(html or "")
    ]


def _parse_archive_html(html):
    """Analyser une page de listing réelle du site: les items (même format
    que le bloc 'Nouveautés') et sa pagination (bloc #bottom-nav), pour en
    tirer le vrai nombre total de pages plutôt que de l'estimer."""
    items = []
    for item in _parse_short_items(html or ""):
        items.append({
            "site": "french-stream",
            "title": item["title"],
            "poster": item["poster"],
            "url": urljoin("https://french-stream.one/", item["href"]),
            "version": item["version"],
        })

    nav_idx = html.find('id="bottom-nav"')
    nav_block = html[nav_idx:nav_idx + 4000] if nav_idx != -1 else ""
    numbers = [int(n) for n in re.findall(r">(\d+)</(?:a|span)>", nav_block)]
    total_pages = max(numbers) if numbers else None
    has_prev = 'class="pprev"' in nav_block
    has_next = 'class="pnext"' in nav_block
    return items, total_pages, has_prev, has_next


def fetch_latest_archive(params, page=1):
    """Lister une page d'archive réelle du site (catégorie "films"/"s-tv" ou
    recherche par genre via xfsearch), en simple requête HTTP: contrairement
    à /engine/ajax/*, ce point d'entrée n'est pas bloqué pour un client non-
    navigateur, et il donne son vrai total de pages."""
    query = dict(params)
    if page and page > 1:
        query["cstart"] = page
    response = requests.get(
        "https://french-stream.one/index.php",
        params=query,
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    response.raise_for_status()
    return _parse_archive_html(response.text)


# Synopsis + note + genre/durée: tout est déjà dans le DOM rendu de la page
# film/série (pas d'appel réseau supplémentaire). La note reproduit le calcul
# JS du site (home-carousel-u6.js: calcRating) à partir des votes déjà
# présents dans la page, le site ne l'affichant lui-même qu'en JS côté client.
EXTRA_INFO_JS = """
() => {
    const descEl = document.querySelector('.fdesc');
    let synopsis = null;
    if (descEl) {
        const clone = descEl.cloneNode(true);
        const seo = clone.querySelector('.desc-text');
        if (seo) seo.remove();
        synopsis = clone.textContent.replace(/\\s+/g, ' ').trim() || null;
    }
    const genresEl = document.querySelector('.genres');
    const runtimeEl = document.querySelector('.runtime');
    const yearEl = document.querySelector('.release_date');
    const voteWrap = document.querySelector('.fr-votes');
    let rating = null;
    if (voteWrap) {
        const tvEl = voteWrap.querySelector('[id^="vote-num-id-"]');
        const tv = tvEl ? parseInt(tvEl.textContent, 10) : 0;
        const pvEl = voteWrap.querySelector('[class*="ratingtype"]');
        const pv = pvEl ? (parseInt((pvEl.textContent || '').replace('+', ''), 10) || 0) : 0;
        if (tv > 0) rating = Math.round((tv - (tv - pv) / 2) / tv * 100) / 10;
    }
    return {
        synopsis,
        genres: genresEl ? genresEl.textContent.trim() : null,
        runtime: runtimeEl ? runtimeEl.textContent.replace('-', '').trim() : null,
        year: yearEl ? yearEl.textContent.replace('-', '').trim() : null,
        rating,
    };
}
"""


class FrenchStreamScraper:
    def __init__(self, headless=True):
        self.headless = headless

    def search(self, browser, query):
        """Interroger le moteur de recherche interne du site (AJAX, utilisé
        par sa barre de recherche en direct). Doit passer par un vrai
        navigateur: le site bloque silencieusement (redirection) les requêtes
        HTTP brutes envoyées hors d'un contexte navigateur (anti-bot)."""
        page = browser.new_page(user_agent=USER_AGENT)
        try:
            page.goto("https://french-stream.one/", wait_until="domcontentloaded", timeout=30000)
            raw = page.evaluate(
                """async (q) => {
                    const r = await fetch('/engine/ajax/search.php', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: 'query=' + encodeURIComponent(q) + '&page=1',
                    });
                    return await r.text();
                }""",
                query,
            )
            results = []
            for href, poster, alt, title in FS_SEARCH_ITEM_RE.findall(raw or ""):
                # Les pages séries ont "-saison-" dans leur URL (ex: .../furious-saison-1-2026.html),
                # les films non: c'est le seul indice fiable disponible à ce stade.
                content_type = "serie" if "saison" in href.lower() else "film"
                results.append({
                    "site": "french-stream",
                    "type": content_type,
                    "title": unescape(title or alt).strip(),
                    "poster": poster or None,
                    "url": urljoin(page.url, href),
                })
            return results[:14]
        except Exception:
            return []
        finally:
            page.close()

    def latest(self, browser, category_id, limit=10, offset=0):
        """Lister les derniers ajouts d'une catégorie donnée via le bloc
        'Nouveautés Films/Séries' de la page d'accueil (son AJAX interne,
        /engine/ajax/custom.php): même mécanisme anti-bot que search(), un
        vrai navigateur est nécessaire. `offset` permet la pagination (page
        suivante = offset + limit)."""
        page = browser.new_page(user_agent=USER_AGENT)
        try:
            page.goto("https://french-stream.one/", wait_until="domcontentloaded", timeout=30000)
            castom = f"custom category='{category_id}' template='shortstory' from='{offset}' limit='{limit}'"
            raw = page.evaluate(
                """async (castom) => {
                    const r = await fetch('/engine/ajax/custom.php', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: 'castom=' + encodeURIComponent(castom),
                    });
                    return await r.text();
                }""",
                castom,
            )
            items = []
            for item in _parse_short_items(raw or ""):
                items.append({
                    "site": "french-stream",
                    "title": item["title"],
                    "poster": item["poster"],
                    "url": urljoin(page.url, item["href"]),
                    "version": item["version"],
                })
            return items
        except Exception:
            return []
        finally:
            page.close()

    def extract_episodes(self, url):
        """Ouvrir la page dans un navigateur headless, laisser le JS charger
        les épisodes, puis extraire les données depuis le DOM rendu."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless, args=LOW_MEMORY_CHROMIUM_ARGS)
            page = browser.new_page(user_agent=USER_AGENT)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_selector(
                        "#vf-episodes .episode-row, #vostfr-episodes .episode-row",
                        timeout=20000,
                    )
                except Exception:
                    pass  # aucune ligne d'épisode n'est apparue à temps

                meta = page.evaluate(
                    """() => {
                        const el = document.querySelector('#serie-data')
                            || document.querySelector('h1');
                        if (el && el.dataset && el.dataset.title) {
                            return { title: el.dataset.title, poster: el.dataset.affiche || null };
                        }
                        return { title: el ? el.textContent.trim() : 'Titre inconnu', poster: null };
                    }"""
                )

                extra = page.evaluate(EXTRA_INFO_JS)
                episodes = {
                    "series_title": meta["title"],
                    "poster": meta["poster"],
                    "url": url,
                    "synopsis": extra.get("synopsis"),
                    "rating": extra.get("rating"),
                    "genres": extra.get("genres"),
                    "runtime": extra.get("runtime"),
                    "year": extra.get("year"),
                    "vf": [],
                    "vostfr": [],
                }
                for language, container_id in (("vf", "vf-episodes"), ("vostfr", "vostfr-episodes")):
                    rows = page.evaluate(
                        """(containerId) => {
                            const container = document.getElementById(containerId);
                            if (!container) return [];
                            return Array.from(container.querySelectorAll('.episode-row')).map(row => ({
                                text: row.textContent.trim(),
                                html: row.outerHTML,
                            }));
                        }""",
                        container_id,
                    )
                    for index, row in enumerate(rows, start=1):
                        match = re.search(r"(\d+)", row["text"])
                        number = int(match.group(1)) if match else index
                        episodes[language].append({
                            "number": number,
                            "title": row["text"],
                            "url": url,
                        })
                    episodes[language].sort(key=lambda episode: episode["number"])

                return episodes
            finally:
                browser.close()

    def get_direct_download_url(self, page, language, episode_number, quality="auto"):
        """Lire les liens hébergeurs de l'épisode directement depuis l'objet JS
        du site (window.fssGetEpData()/episodesData) et résoudre le lien MP4
        direct via l'hébergeur vidzy. Le site a remplacé l'ancien panneau de
        téléchargement (.ep-dl-panel .ep-dl-primary) par une modale JS qui
        n'écrit rien dans le DOM avant d'être cliquée, et le clic lui-même
        déclenche souvent une popup publicitaire qui casse Playwright: lire
        les données directement évite ces deux problèmes."""
        data = page.evaluate(
            """() => {
                try {
                    if (typeof window.fssGetEpData === 'function') return window.fssGetEpData();
                } catch (e) {}
                return window.episodesData || null;
            }"""
        )
        episode = (data or {}).get(language, {}).get(str(episode_number))
        if not episode:
            return None

        vidzy_embed = episode.get("vidzy")
        if not vidzy_embed:
            return None

        match = re.search(r"vidzy\.[a-z]+/embed-([a-zA-Z0-9]+)\.html", vidzy_embed)
        if not match:
            return None

        host_page_url = f"https://vidzy.org/d/{match.group(1)}.html"
        for _ in range(3):
            result = self.resolve_host_download(page.context.browser, host_page_url, quality=quality)
            if result:
                return result
        return None

    def get_movie_options(self, url):
        """Ouvrir la page d'un film et récupérer le lien hébergeur du lecteur
        intégré. L'ancien bouton #downloadBtn est mort côté site (plus aucun
        gestionnaire JS ne le relie à #downloadOptions); le lecteur principal
        charge vidzy par défaut et un script du site réécrit systématiquement
        son iframe vers vidzy.org, donc lire son src donne le même lien que
        l'ancien mécanisme de téléchargement."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless, args=LOW_MEMORY_CHROMIUM_ARGS)
            page = browser.new_page(user_agent=USER_AGENT)
            page.on("popup", lambda popup: popup.close())
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                meta = page.evaluate(
                    """() => {
                        const el = document.querySelector('#film-data') || document.querySelector('h1');
                        if (el && el.dataset && el.dataset.title) {
                            return { title: el.dataset.title, poster: el.dataset.affiche || null };
                        }
                        return { title: el ? el.textContent.trim() : 'Titre inconnu', poster: null };
                    }"""
                )
                title, poster = meta["title"], meta["poster"]
                extra = page.evaluate(EXTRA_INFO_JS)

                # Le lecteur réel ne se charge qu'après un clic sur le bouton
                # "play" (invisible, superposé à la bande-annonce qui joue en
                # fond); sans ce clic, seul l'iframe de la bande-annonce apparaît.
                # Un court délai est nécessaire pour laisser le JS du site
                # attacher son gestionnaire de clic avant qu'on ne clique.
                page.wait_for_timeout(1500)
                play_button = page.query_selector(".play-button")
                if play_button:
                    try:
                        play_button.click(force=True)
                    except Exception:
                        pass

                # Le site utilise au moins deux gabarits différents selon le
                # film: parfois le lecteur apparaît dans #main-player après le
                # clic "play", parfois il n'apparaît qu'ailleurs dans la page
                # (ex: <iframe id="video-iframe">) et seul un clic direct sur
                # le bouton hébergeur ViDZY le charge. On cherche donc
                # n'importe quel iframe pointant vers un hébergeur connu,
                # sans dépendre d'un conteneur ou id précis.
                find_embed_js = """
                    () => {
                        const frames = Array.from(document.querySelectorAll('iframe'));
                        const match = frames.find(f => /vidzy\\.|dood\\.|voe\\./i.test(f.src || ''));
                        return match ? match.src : null;
                    }
                """

                def find_embed_url(attempts=4, delay=1200):
                    url_found = None
                    for _ in range(attempts):
                        url_found = page.evaluate(find_embed_js)
                        if url_found and "vidzy." in url_found:
                            return url_found
                        page.wait_for_timeout(delay)
                    return url_found

                embed_url = find_embed_url()

                if not embed_url or "vidzy." not in embed_url:
                    # Soit aucun lecteur ne s'est chargé, soit un autre hébergeur
                    # que vidzy est actif par défaut: on force ViDZY, le seul
                    # hébergeur que resolve_host_download sait résoudre.
                    vidzy_button = page.query_selector('.player-option[data-player="ViDZY"]')
                    if vidzy_button:
                        try:
                            vidzy_button.click(force=True)
                        except Exception:
                            pass
                        embed_url = find_embed_url()

                info = {
                    "title": title,
                    "poster": poster,
                    "synopsis": extra.get("synopsis"),
                    "rating": extra.get("rating"),
                    "genres": extra.get("genres"),
                    "runtime": extra.get("runtime"),
                    "year": extra.get("year"),
                }

                if not embed_url or "vidzy." not in embed_url:
                    return {**info, "options": []}

                embed_url = embed_url.split("?")[0]
                host_page_url = embed_url.replace("/embed-", "/d/")
                options = self.list_host_qualities(browser, host_page_url)
                return {**info, "options": options}
            finally:
                browser.close()

    def list_host_qualities(self, browser, host_page_url):
        """Lister les niveaux de qualité réellement proposés par la page
        hébergeur (liens _n/_o), sans aller jusqu'à résoudre le MP4 final.
        Ne renvoie que ce qui existe vraiment pour cette vidéo: jamais une
        liste de résolutions inventée."""
        host_page = browser.new_page()
        try:
            host_page.goto(host_page_url, wait_until="domcontentloaded", timeout=30000)
            host_page.wait_for_timeout(1500)

            options = []
            for key, suffix in QUALITY_SUFFIXES.items():
                link = host_page.query_selector(f'a[href*="{suffix}"]')
                href = link.get_attribute("href") if link else None
                if href:
                    options.append({
                        "label": QUALITY_LABELS[key],
                        "data_url": urljoin(host_page.url, href),
                    })
            if not options:
                # Aucun lien de qualité distinct trouvé: un seul niveau existe,
                # on propose directement la page hébergeur telle quelle.
                options.append({"label": "Qualité disponible", "data_url": host_page_url})
            return options
        except Exception:
            return [{"label": "Qualité disponible", "data_url": host_page_url}]
        finally:
            host_page.close()

    def resolve_movie_download(self, browser, data_url):
        """Résoudre le MP4 direct à partir d'un lien de qualité choisi (renvoyé
        par list_host_qualities) ou, par compatibilité, d'un ancien lien embed-."""
        host_page_url = data_url.replace("/embed-", "/d/") if "/embed-" in data_url else data_url
        for attempt in range(3):
            result = self.resolve_host_download(browser, host_page_url)
            if result:
                return result
        return None

    def resolve_host_download(self, browser, host_page_url, quality="auto"):
        """Suivre la chaîne de pages d'un hébergeur (ex: vidzy.cc) jusqu'au lien
        MP4 direct. Si `quality` demande un niveau précis et qu'il existe, on le
        prend; sinon on se rabat sur le premier lien de qualité disponible
        plutôt que d'échouer (certains épisodes n'ont qu'un seul niveau)."""
        if ".mp4" in host_page_url.split("?")[0]:
            # list_host_qualities a parfois déjà résolu un lien direct vers le
            # CDN (pas une page de confirmation à suivre): le re-naviguer et
            # chercher un bouton dessus échouerait, puisque ce n'est pas du HTML.
            return {"url": host_page_url, "referer": host_page_url}

        host_page = browser.new_page()
        try:
            host_page.goto(host_page_url, wait_until="domcontentloaded", timeout=30000)
            host_page.wait_for_timeout(1500)

            quality_link = None
            suffix = QUALITY_SUFFIXES.get(quality)
            if suffix:
                quality_link = host_page.query_selector(f'a[href*="{suffix}"]')
            if not quality_link:
                quality_link = host_page.query_selector('a[href*="_n"], a[href*="_o"]')
            resolve_url = quality_link.get_attribute("href") if quality_link else None

            if resolve_url and ".mp4" in resolve_url:
                return {"url": resolve_url, "referer": host_page.url}

            if resolve_url and resolve_url != host_page.url:
                host_page.goto(resolve_url, wait_until="domcontentloaded", timeout=30000)

            # Le formulaire de la page se soumet automatiquement en JS et révèle
            # ensuite le vrai lien MP4 dans un <a class="main-button" href="...mp4">.
            host_page.wait_for_selector('a.main-button[href*=".mp4"]', timeout=20000)
            direct_link = host_page.query_selector('a.main-button[href*=".mp4"]')
            direct_url = direct_link.get_attribute("href") if direct_link else None
            return {"url": direct_url, "referer": host_page.url} if direct_url else None
        except Exception:
            return None
        finally:
            host_page.close()


# ---------------------------------------------------------------------------
# PORNO (fr.pornhub.com / www.pornhub.com)
#
# La page du lecteur expose, dans son HTML, un objet JS `flashvars_<id>` qui
# contient `mediaDefinitions`: la liste réelle des flux proposés pour la vidéo
# (MP4 de plusieurs résolutions et/ou un flux HLS adaptatif), avec pour chacun
# sa vraie résolution et un éventuel flag `premium`. On lit directement ces
# données: pas d'URL de CDN devinée, et donc pas de casse quand le CDN change.
# Un repli via navigateur est prévu si la page est servie en JS ou renvoie 403.
# ---------------------------------------------------------------------------

PORNO_FLASHVARS_RE = re.compile(r"var\s+flashvars_\d+\s*=")
PORNO_VIEWKEY_RE = re.compile(r"viewkey=([A-Za-z0-9_\-]+)")


def _porno_normalize_url(url):
    """Accepter indifféremment une URL view_video.php, une URL /embed/<key>
    ou juste une viewkey: on ne travaille ensuite que sur une seule forme."""
    url = url.strip()
    if "/embed/" in url:
        key = url.rstrip("/").split("/embed/")[-1].split("?")[0].split("#")[0]
        return f"https://www.pornhub.com/view_video.php?viewkey={key}"
    if "viewkey=" not in url:
        match = PORNO_VIEWKEY_RE.search(url)
        if match:
            return f"https://www.pornhub.com/view_video.php?viewkey={match.group(1)}"
    return url


def _porno_extract_balanced_object(text, start):
    """Isoler un littéral objet JS à partir de son '{' en tenant compte des
    chaînes (les accolades à l'intérieur d'une string ne doivent pas être
    comptées). Le flashvars est du JSON valide: une fois l'objet isolé, il
    se charge avec json.loads."""
    depth = 0
    in_string = False
    escaped = False
    quote = ""
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in "\"'":
            in_string = True
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _porno_parse_flashvars(html):
    match = PORNO_FLASHVARS_RE.search(html or "")
    if not match:
        return None
    brace = html.find("{", match.end())
    if brace == -1:
        return None
    blob = _porno_extract_balanced_object(html, brace)
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def _porno_meta(html, prop):
    """Lire une balise <meta property="..."> quel que soit l'ordre des
    attributs (property avant ou après content)."""
    for tag_match in re.finditer(r"<meta\b[^>]*>", html or "", re.I):
        tag = tag_match.group(0)
        if f'property="{prop}"' in tag or f"property='{prop}'" in tag:
            content_match = re.search(r'content=["\']([^"\']*)["\']', tag, re.I)
            if content_match:
                return content_match.group(1)
    return None


def _porno_format_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return None
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _porno_parse_media_definitions(definitions):
    """Ne garder que ce qui est réellement téléchargeable, dédupliqué par
    qualité, trié de la meilleure à la moins bonne."""
    options = []
    seen = set()
    for entry in definitions or []:
        url = (entry.get("videoUrl") or "").strip()
        if not url:
            continue
        quality = str(entry.get("quality") or "").strip()
        fmt = (entry.get("format") or "").lower()

        if fmt == "hls" or ".m3u8" in url:
            kind = "hls"
            label = "HLS (adaptatif, meilleure qualité disponible)"
        elif "/video/get_media" in url:
            # Pornhub route désormais son format "mp4" vers ce point d'API au
            # lieu d'un lien CDN direct: une requête simple y renvoie "[]"
            # (probablement un challenge anti-bot côté site), donc ce lien ne
            # mène jamais à un vrai fichier téléchargeable. On l'ignore plutôt
            # que de proposer une option qui produit un fichier de 2 octets.
            continue
        elif quality.isdigit():
            kind = "mp4"
            label = _label_for_height(int(quality))
        else:
            kind = "mp4"
            label = "Qualité disponible"

        key = (kind, label)
        if key in seen:
            continue
        seen.add(key)
        options.append({
            "label": label,
            "url": url,
            "type": kind,
            "premium": bool(entry.get("premium")),
            "height": int(quality) if quality.isdigit() else 0,
        })

    # MP4 triés par résolution décroissante; le HLS adaptatif en dernier (il
    # n'a pas de résolution propre, il s'adapte au débit).
    options.sort(key=lambda option: (option["type"] == "hls", -option["height"]))
    return options


PORNO_LIST_ITEM_HREF_RE = re.compile(r'href="(/view_video\.php\?viewkey=[^"]+)"')
PORNO_LIST_ITEM_IMG_RE = re.compile(r'data-image="([^"]+)"')
PORNO_LIST_ITEM_IMG_FALLBACK_RE = re.compile(r'<img[^>]*\bsrc="([^"]+)"')
PORNO_LIST_ITEM_DURATION_RE = re.compile(r'<var class="duration">([^<]*)</var>')
PORNO_LIST_ITEM_TITLE_RE = re.compile(r'<span class="title">.*?<a[^>]*>([^<]*)</a>', re.DOTALL)
PORNO_LIST_ITEM_VIEWS_RE = re.compile(r'<span class="views"><var>([^<]*)</var>')
PORNO_NEXT_PAGE_RE = re.compile(r'<link rel="next"')


def _parse_porno_list_items(html):
    """Analyser une page de listing/recherche pornhub (gabarit commun aux deux):
    chaque vignette est un <li class="pcVideoListItem ...">, qu'on découpe en
    chunks pour ne jamais associer les métadonnées d'une vignette à sa voisine."""
    items = []
    for chunk in (html or "").split('<li class="pcVideoListItem')[1:]:
        href_match = PORNO_LIST_ITEM_HREF_RE.search(chunk)
        if not href_match:
            continue
        title_match = PORNO_LIST_ITEM_TITLE_RE.search(chunk)
        img_match = PORNO_LIST_ITEM_IMG_RE.search(chunk) or PORNO_LIST_ITEM_IMG_FALLBACK_RE.search(chunk)
        duration_match = PORNO_LIST_ITEM_DURATION_RE.search(chunk)
        views_match = PORNO_LIST_ITEM_VIEWS_RE.search(chunk)
        items.append({
            "title": unescape(title_match.group(1)).strip() if title_match else "Sans titre",
            "poster": img_match.group(1) if img_match else None,
            "url": urljoin("https://www.pornhub.com/", href_match.group(1)),
            "duration": duration_match.group(1).strip() if duration_match else None,
            "views": views_match.group(1).strip() if views_match else None,
        })
    return items


class PornoScraper:
    def __init__(self, headless=True):
        self.headless = headless

    def _fetch_html(self, url, params=None):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        }
        # Cookies de contournement de la page d'âge et de la bannière de
        # consentement: sans eux la page peut être tronquée avant le lecteur.
        cookies = {"age_verified": "1", "platform": "pc", "cookieconsent": "1"}
        response = requests.get(url, params=params, headers=headers, cookies=cookies, timeout=30)
        response.raise_for_status()
        return response.text

    def _fetch_html_via_browser(self, url):
        """Repli quand le HTML brut ne contient pas le flashvars (page servie
        en JS) ou que le serveur bloque la requête directe."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless, args=LOW_MEMORY_CHROMIUM_ARGS)
            page = browser.new_page(user_agent=USER_AGENT)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                return page.content()
            finally:
                browser.close()

    def extract_video(self, url):
        url = _porno_normalize_url(url)
        html = None
        try:
            html = self._fetch_html(url)
        except requests.RequestException:
            html = None

        data = _porno_parse_flashvars(html) if html else None
        if not data:
            html = self._fetch_html_via_browser(url)
            data = _porno_parse_flashvars(html)
        if not data:
            return None

        options = _porno_parse_media_definitions(data.get("mediaDefinitions"))
        if not options:
            return None

        title = _porno_meta(html, "og:title") or data.get("video_title") or "video"
        title = re.sub(r"\s*-\s*Pornhub\.com\s*$", "", title).strip() or "video"

        return {
            "title": title,
            "thumbnail": _porno_meta(html, "og:image"),
            "duration": _porno_format_duration(_porno_meta(html, "video:duration")),
            "url": url,
            "options": options,
        }

    def search_videos(self, query=None, page=1, category=None, sort=None, hd=False, min_duration=None, max_duration=None):
        """Lister les vidéos du site: recherche par mot-clé si fourni, sinon
        parcours du catalogue général (/video), filtrable par catégorie
        réelle du site (voir PORNO_CATEGORIES). Les deux pages partagent le
        même gabarit de vignette et le même mécanisme de pagination. Comme
        pour les listings films/séries, catégorie et recherche par mot-clé ne
        se combinent pas (le filtre catégorie n'a de sens qu'en parcours).

        Le filtre avancé (tri, HD, durée) est délégué au site lui-même via
        ses propres paramètres d'URL plutôt que filtré côté client: on ne
        reçoit qu'une page de résultats à la fois, un filtrage local ne
        porterait donc que sur cette page et non sur tout le catalogue."""
        params = {"page": page}
        if query:
            params["search"] = query
            base_url = "https://www.pornhub.com/video/search"
        elif category and category.startswith("kw:"):
            # Catégorie non listée sur le site (ex: "Femme enceinte"): pas
            # d'identifiant de catégorie réel, on la fait passer par la
            # recherche par mot-clé plutôt que d'inventer un `c=` qui n'existe pas.
            params["search"] = category[len("kw:"):]
            base_url = "https://www.pornhub.com/video/search"
        else:
            base_url = "https://www.pornhub.com/video"
            if category and category != "all":
                params["c"] = category
        if sort in PORNO_SORT_VALUES:
            params["o"] = sort
        if hd:
            params["hd"] = 1
        if min_duration:
            params["min_duration"] = min_duration
        if max_duration:
            params["max_duration"] = max_duration
        html = self._fetch_html(base_url, params=params)
        items = _parse_porno_list_items(html)
        has_next = bool(PORNO_NEXT_PAGE_RE.search(html or ""))
        return items, has_next


class FootballScraper:
    """Lister les matchs et résoudre le flux .m3u8, par détection STRUCTURELLE
    (heure + score), sans dépendre des classes CSS ni du motif d'URL.

    `debug=True` (ou FOOTBALL_DEBUG=1 dans l'environnement) affiche, pour
    chaque étape, ce qui a été trouvé et écrit le DOM sur disque quand rien
    ne remonte — indispensable pour savoir *pourquoi* une liste est vide."""

    def __init__(self, headless=True, base=FOOTBALL_BASE, locale=FOOTBALL_LOCALE, debug=False):
        self.headless = headless
        self.base = base.rstrip("/")
        self.locale = locale
        self.debug = debug

    # -- internes ---------------------------------------------------------
    def _candidate_urls(self):
        """Le préfixe de locale n'est pas fiable sur ce réseau de domaines
        (les URLs indexées sont sans locale): on essaie les deux formes et on
        garde la première qui rend des lignes."""
        urls = []
        for loc in (self.locale, ""):
            for path in FOOTBALL_SPORT_PATHS:
                urls.append(f"{self.base}{loc}{path}")
        return list(dict.fromkeys(urls))

    def _new_page(self, playwright):
        browser = playwright.chromium.launch(headless=self.headless, args=CHROMIUM_ARGS)
        page = browser.new_page(user_agent=USER_AGENT, locale="fr-FR",
                                viewport={"width": 1440, "height": 900})
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        return browser, page

    def _settle(self, page, networkidle=True):
        """Laisser la SPA charger. On n'utilise volontairement PAS
        wait_for_selector: il attend par défaut un élément VISIBLE, or le site
        masque ses blocs tant que le JS n'a pas fini -> timeout garanti alors
        que les lignes existent déjà dans le DOM."""
        if networkidle:
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
        for _ in range(3):
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                break
            page.wait_for_timeout(1000)
        try:
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        page.wait_for_timeout(400)

    def _collect(self, page):
        """Le listing peut vivre dans une iframe: on évalue sur toutes les
        frames et on préfixe l'index par le numéro de frame, pour pouvoir
        recliquer exactement la même ligne depuis get_stream()."""
        rows = []
        for frame_index, frame in enumerate(page.frames):
            try:
                part = frame.evaluate(FOOTBALL_MATCHES_JS)
            except Exception:
                continue
            for row_index, row in enumerate(part or []):
                row["key"] = f"{frame_index}:{row_index}"
                rows.append(row)
        return rows

    def _click_row(self, page, key):
        try:
            frame_index, row_index = (int(part) for part in key.split(":"))
        except ValueError:
            return False
        if frame_index >= len(page.frames):
            return False
        try:
            return bool(page.frames[frame_index].evaluate(FOOTBALL_CLICK_JS, row_index))
        except Exception:
            return False

    # -- API --------------------------------------------------------------
    def list_matches(self, only_live=False, include_finished=False, limit=60):
        """Renvoie les matchs affichés (en direct + à venir, ou seulement live).

        `only_live` s'appuie sur le badge minute (44', 65', 90+'): sur
        l'onglet Live du site le score est affiché pour tout le monde, donc ce
        n'est pas lui qui distingue un match en cours d'un match à venir."""
        with sync_playwright() as playwright:
            browser, page = self._new_page(playwright)
            rows = []
            try:
                for candidate in self._candidate_urls():
                    try:
                        page.goto(candidate, wait_until="domcontentloaded", timeout=45000)
                    except Exception as error:
                        if self.debug:
                            print(f"  {Colors.DIM}[debug] {candidate} -> {error}{Colors.RESET}")
                        continue
                    self._settle(page)
                    rows = self._collect(page)
                    if self.debug:
                        print(f"  {Colors.DIM}[debug] {candidate} -> {len(rows)} ligne(s){Colors.RESET}")
                    if rows:
                        break
                if self.debug and not rows:
                    _football_dump(page)
            finally:
                browser.close()

        matches, seen = [], set()
        for row in rows:
            text = (row.get("text") or "").strip()
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())

            live = bool(row.get("minute"))
            finished = bool(re.search(r"\b(termine|terminé|ft|ended|ap)\b", text, re.I))
            if only_live and not live:
                continue
            if not include_finished and finished and not live:
                continue

            if row.get("home") and row.get("away"):
                title = f"{row['home']} - {row['away']}"
            else:
                title = text[:110]

            matches.append({
                "title": title[:110],
                "league": row.get("league"),
                "time": row.get("time"),
                "minute": row.get("minute"),
                "score": row.get("score"),
                "live": live,
                "url": row.get("href"),
                "key": row["key"],
            })

        # Repli: aucune ligne "live" détectée alors que des matchs existent ->
        # on renvoie quand même la liste au lieu d'un "aucun match" sec.
        if not matches and rows and only_live:
            return self.list_matches(only_live=False, include_finished=True, limit=limit)
        return matches[:limit]

    def _read_player_state(self, page, timeout_ms=30000):
        """Attendre que le lecteur publie le flux. Aucun clic 'play' n'est
        nécessaire en principe: le store est rempli par fetchStreamDetail
        AVANT la lecture."""
        waited = 0
        while waited < timeout_ms:
            for frame in page.frames:
                try:
                    data = frame.evaluate(FOOTBALL_STORE_JS)
                except Exception:
                    continue
                url = (data or {}).get("mil") or (data or {}).get("videoSrc")
                if url:
                    return {
                        "url": url,
                        "server": data.get("name"),
                        "servers": data.get("servers") or [],
                    }
            page.wait_for_timeout(1000)
            waited += 1000
        return None

    def _sniff_m3u8(self, page, captured):
        """Repli qui ne dépend PAS de la structure du store: on lit les
        requêtes réseau déjà émises (.m3u8). C'est le filet de sécurité si le
        store change de forme ou vit dans une autre application JS."""
        for frame in page.frames:
            try:
                urls = frame.evaluate(
                    "() => performance.getEntriesByType('resource')"
                    ".map(e => e.name).filter(n => n.includes('.m3u8'))"
                )
            except Exception:
                urls = []
            if urls:
                return {"url": urls[-1], "server": None, "servers": []}
        if captured:
            return {"url": captured[-1], "server": None, "servers": []}
        return None

    def get_stream(self, match):
        """Récupérer le flux .m3u8 d'un match.

        `match` est le dictionnaire renvoyé par list_matches (ou une simple
        URL, par compatibilité). Deux chemins: navigation directe si une URL
        de match est connue, sinon ouverture du listing et clic sur la ligne
        (utile quand le lien est porté par l'icône TV et non par un <a href>,
        auquel cas `match["url"]` vaut None et seul `match["key"]` permet de
        retrouver la ligne)."""
        if isinstance(match, str):
            match = {"url": match, "title": match.rsplit("/", 1)[-1]}

        with sync_playwright() as playwright:
            browser, page = self._new_page(playwright)
            context = page.context
            captured = []

            def attach(target):
                target.on("response", lambda response: captured.append(response.url)
                          if ".m3u8" in response.url else None)

            attach(page)
            context.on("page", attach)

            def read(target, timeout):
                return self._read_player_state(target, timeout) or self._sniff_m3u8(target, captured)

            try:
                # 1) URL de match connue -> navigation directe
                if match.get("url"):
                    page.goto(match["url"], wait_until="domcontentloaded", timeout=45000)
                    self._settle(page)
                    result = read(page, 20000)
                    if result:
                        result["referer"] = page.url
                        if self.debug:
                            print(f"  {Colors.DIM}[debug] flux: {result['url']}{Colors.RESET}")
                        return result

                # 2) Repli: ouvrir le listing et cliquer la ligne
                if match.get("key") is not None:
                    for candidate in self._candidate_urls():
                        page.goto(candidate, wait_until="domcontentloaded", timeout=45000)
                        self._settle(page)
                        self._collect(page)          # repose les marqueurs data-fb-idx
                        if not self._click_row(page, match["key"]):
                            continue
                        page.wait_for_timeout(4000)
                        # le clic peut ouvrir un onglet: on suit la nouvelle page
                        target = context.pages[-1] if len(context.pages) > 1 else page
                        result = read(target, 25000)
                        if not result:
                            for frame in target.frames:
                                for selector in ("video", ".vjs-big-play-button",
                                                 "button[class*=play]", ".play-button"):
                                    try:
                                        frame.click(selector, timeout=1500)
                                        break
                                    except Exception:
                                        pass
                            result = read(target, 12000)
                        if result:
                            result["referer"] = target.url
                            if self.debug:
                                print(f"  {Colors.DIM}[debug] flux (clic): {result['url']}{Colors.RESET}")
                            return result

                if self.debug:
                    _football_dump(page, "football_stream_debug.html")
                return None
            except Exception as error:
                if self.debug:
                    print(f"  {Colors.DIM}[debug] get_stream: {error}{Colors.RESET}")
                return None
            finally:
                browser.close()


def football_probe(url=f"{FOOTBALL_BASE}{FOOTBALL_LOCALE}{FOOTBALL_SPORT_PATH}"):
    """Affiche la structure RÉELLE des lignes détectées (texte, équipes,
    liens, extrait HTML). À lancer si l'extraction remonte 0 ou un résultat
    bizarre: donne de quoi ajuster les regex SCORE/TIME/MIN sans deviner."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        page = browser.new_page(user_agent=USER_AGENT, locale="fr-FR",
                                viewport={"width": 1440, "height": 900})
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        scraper = FootballScraper()
        scraper._settle(page)
        rows = scraper._collect(page)
        print(f"URL finale     : {page.url}")
        print(f"Titre          : {page.title()}")
        print(f"Lignes trouvées: {len(rows)}")
        for row in rows[:3]:
            print("-" * 60)
            print("équipes :", row.get("home"), "|", row.get("away"))
            print("heure   :", row.get("time"), "| minute:", row.get("minute"), "| score:", row.get("score"))
            print("ligue   :", row.get("league"))
            print("liens   :", row.get("links"))
            print("html    :", row.get("html"))
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        print(f"\nTotal <a[href]> sur la page: {len(hrefs)}")
        print("Exemples:", hrefs[:25])
        _football_dump(page, "football_debug.html")
        browser.close()


# ---------------------------------------------------------------------------
# Téléchargement HLS (.m3u8): de plus en plus d'hébergeurs (uqload, ansembed,
# smoothpre...) ne servent plus de MP4 direct, seulement un flux segmenté.
# On récupère tous les segments puis on les remuxe en .mp4 via ffmpeg s'il est
# installé (copie de flux, aucun ré-encodage, donc rapide et sans perte); sans
# ffmpeg, on livre quand même le résultat sous forme de .ts (lisible par VLC
# et la plupart des lecteurs, juste pas par le lecteur intégré du navigateur).
# ---------------------------------------------------------------------------

def _resolve_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    # PATH n'est mis à jour qu'au redémarrage du terminal après une
    # installation via winget: on vérifie aussi directement son dossier
    # d'installation habituel pour que ça marche sans relancer l'app.
    winget_root = os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "Microsoft", "WinGet", "Packages"
    )
    if not os.path.isdir(winget_root):
        return None
    for name in os.listdir(winget_root):
        if not name.lower().startswith(("gyan.ffmpeg", "btbn.ffmpeg")):
            continue
        for dirpath, _dirs, files in os.walk(os.path.join(winget_root, name)):
            if "ffmpeg.exe" in files:
                return os.path.join(dirpath, "ffmpeg.exe")
    return None


_M3U8_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")


def _parse_m3u8_variants(text, base_url):
    """Variantes de qualité d'une master playlist, triées de la meilleure à
    la moins bonne (on choisit la plus haute bande passante)."""
    variants = []
    pending_bandwidth = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            match = _M3U8_BANDWIDTH_RE.search(line)
            pending_bandwidth = int(match.group(1)) if match else 0
        elif line and not line.startswith("#"):
            if pending_bandwidth is not None:
                variants.append((pending_bandwidth, urljoin(base_url, line)))
                pending_bandwidth = None
    return sorted(variants, key=lambda item: item[0], reverse=True)


def _parse_m3u8_segments(text, base_url):
    return [urljoin(base_url, line.strip()) for line in text.splitlines() if line.strip() and not line.startswith("#")]


_M3U8_MAP_URI_RE = re.compile(r'#EXT-X-MAP:URI="([^"]+)"')


def _parse_m3u8_init_segment(text, base_url):
    """URL du segment d'initialisation ('#EXT-X-MAP'), utilisé par les flux
    HLS en fMP4/CMAF (de plus en plus courants, notamment chez pornhub):
    contrairement au MPEG-TS classique, chaque segment média (.m4s) n'est
    pas lisible seul, il dépend des boîtes ftyp/moov de ce segment initial.
    Absent sur un flux HLS/MPEG-TS classique (segments .ts autonomes)."""
    match = _M3U8_MAP_URI_RE.search(text)
    return urljoin(base_url, match.group(1)) if match else None


def download_hls(playlist_url, dest_path, referer=None, on_progress=None, control=None):
    """Télécharger un flux HLS: résoudre la meilleure variante si la playlist
    est une master playlist, puis télécharger tous les segments dans l'ordre.

    Deux cas selon le flux:
    - fMP4/CMAF (présence de '#EXT-X-MAP'): le segment d'initialisation suivi
      des segments média, concaténés dans l'ordre, forme directement un
      .mp4 fragmenté valide — aucun remux ffmpeg n'est nécessaire.
    - MPEG-TS classique: chaque segment .ts est autonome: on les concatène
      dans un fichier .ts puis on remuxe en .mp4 via ffmpeg s'il est
      disponible (sinon on livre le .ts brut, lisible par VLC).

    Renvoie le chemin réel du fichier produit (peut différer de `dest_path`
    si on est retombé sur du .ts faute de ffmpeg)."""
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer

    response = requests.get(playlist_url, headers=headers, timeout=20)
    response.raise_for_status()
    text = response.text

    if "#EXT-X-STREAM-INF" in text:
        variants = _parse_m3u8_variants(text, playlist_url)
        if not variants:
            raise ValueError("Aucune variante de qualité trouvée dans la playlist HLS.")
        playlist_url = variants[0][1]
        response = requests.get(playlist_url, headers=headers, timeout=20)
        response.raise_for_status()
        text = response.text

    if "#EXT-X-KEY" in text and "METHOD=NONE" not in text:
        raise ValueError("Flux HLS chiffré: non supporté.")

    segment_urls = _parse_m3u8_segments(text, playlist_url)
    if not segment_urls:
        raise ValueError("Playlist HLS vide.")
    init_url = _parse_m3u8_init_segment(text, playlist_url)

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    is_fmp4 = init_url is not None
    out_path = dest_path if is_fmp4 else os.path.splitext(dest_path)[0] + ".part.ts"

    def fetch_segment(segment_url):
        with requests.get(segment_url, headers=headers, timeout=30) as segment_response:
            segment_response.raise_for_status()
            return segment_response.content

    downloaded = 0
    total_segments = len(segment_urls) + (1 if init_url else 0)
    completed = 0

    def write_chunk(output_file, data):
        nonlocal downloaded, completed
        output_file.write(data)
        downloaded += len(data)
        completed += 1
        if control is not None:
            control["quality_label"] = f"HLS · segment {completed}/{total_segments}"
            # Pas de taille totale en octets connue pour du HLS: on expose le
            # ratio de segments pour que la barre de progression côté
            # interface avance en direct au lieu d'attendre la fin.
            control["segments_done"] = completed
            control["segments_total"] = total_segments
        if on_progress:
            on_progress(downloaded, 0)

    # Un segment HLS dure quelques secondes: les télécharger un par un
    # enchaîne des centaines d'allers-retours réseau en série (très lent).
    # On les télécharge en parallèle (plusieurs connexions à la fois) mais on
    # les écrit dans l'ordre d'origine, seul ordre qui donne une vidéo valide.
    with open(out_path, "wb") as output_file, ThreadPoolExecutor(max_workers=6) as executor:
        if init_url:
            write_chunk(output_file, fetch_segment(init_url))
        futures = [executor.submit(fetch_segment, segment_url) for segment_url in segment_urls]
        for future in futures:
            if control is not None:
                while control.get("paused"):
                    time.sleep(0.3)
            write_chunk(output_file, future.result())

    if is_fmp4:
        return out_path

    ffmpeg_path = _resolve_ffmpeg()
    if not ffmpeg_path:
        final_path = os.path.splitext(dest_path)[0] + ".ts"
        os.replace(out_path, final_path)
        return final_path

    try:
        subprocess.run(
            [ffmpeg_path, "-y", "-loglevel", "error", "-i", out_path, "-c", "copy", dest_path],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        # Le remux a échoué (flux abîmé/segment manquant...): on livre quand
        # même le .ts brut plutôt que de perdre tout le téléchargement.
        final_path = os.path.splitext(dest_path)[0] + ".ts"
        os.replace(out_path, final_path)
        return final_path
    finally:
        if os.path.isfile(out_path):
            os.remove(out_path)
    return dest_path


def record_live_hls(playlist_url, dest_path, referer=None, seconds=1800, on_progress=None, control=None):
    """Enregistrer un flux HLS EN DIRECT pendant `seconds` secondes.

    download_hls() lit la playlist une seule fois: sur un match en cours, la
    régie la réécrit en continu, donc l'enregistrement s'arrêterait au bout de
    quelques minutes. Ici on relit la playlist en boucle et on n'ajoute que
    les segments jamais vus, jusqu'à la durée demandée. Renvoie le chemin
    réellement produit (.mp4 si le flux est fMP4/CMAF ou si ffmpeg est là,
    sinon .ts brut)."""
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer

    response = requests.get(playlist_url, headers=headers, timeout=20)
    response.raise_for_status()
    text = response.text
    if "#EXT-X-STREAM-INF" in text:
        variants = _parse_m3u8_variants(text, playlist_url)
        if not variants:
            raise ValueError("Aucune variante de qualité trouvée dans la playlist HLS.")
        playlist_url = variants[0][1]

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    started = time.time()
    seen = set()
    is_fmp4 = False
    init_written = False
    out_path = dest_path

    with open(out_path, "wb") as output_file:
        while time.time() - started < seconds:
            try:
                playlist = requests.get(playlist_url, headers=headers, timeout=20).text
            except requests.RequestException:
                time.sleep(2)
                continue

            init_url = _parse_m3u8_init_segment(playlist, playlist_url)
            if init_url and not init_written:
                # fMP4/CMAF: le segment d'init (ftyp/moov) doit être écrit une
                # seule fois, en tête, sinon rien n'est lisible.
                is_fmp4 = True
                with requests.get(init_url, headers=headers, timeout=30) as segment:
                    output_file.write(segment.content)
                init_written = True

            wrote = False
            for segment_url in _parse_m3u8_segments(playlist, playlist_url):
                if segment_url in seen:
                    continue
                seen.add(segment_url)
                try:
                    with requests.get(segment_url, headers=headers, timeout=30) as segment:
                        output_file.write(segment.content)
                except requests.RequestException:
                    continue
                wrote = True
                if on_progress:
                    on_progress(output_file.tell(), 0)
            output_file.flush()

            if control is not None:
                elapsed = int(time.time() - started)
                control["quality_label"] = f"LIVE · {elapsed}s / {seconds}s"
                while control.get("paused"):
                    time.sleep(0.3)
            time.sleep(1 if wrote else 3)

    if is_fmp4:
        return out_path

    # MPEG-TS concaténé brut: remux en .mp4 si ffmpeg est là, sinon .ts brut.
    ffmpeg_path = _resolve_ffmpeg()
    if not ffmpeg_path:
        final_path = os.path.splitext(dest_path)[0] + ".ts"
        os.replace(out_path, final_path)
        return final_path

    tmp_path = dest_path + ".tmp.mp4"
    try:
        subprocess.run(
            [ffmpeg_path, "-y", "-loglevel", "error", "-i", out_path, "-c", "copy", tmp_path],
            check=True,
            capture_output=True,
        )
        os.replace(tmp_path, dest_path)
    except subprocess.CalledProcessError:
        final_path = os.path.splitext(dest_path)[0] + ".ts"
        os.replace(out_path, final_path)
        return final_path
    return dest_path


def download_file(url, dest_path, referer=None, on_progress=None, control=None):
    """Télécharger un fichier en flux avec une barre de progression simple.

    on_progress(downloaded_bytes, total_bytes), si fourni, est appelé à chaque
    chunk reçu (utilisé par l'interface web pour afficher une progression).

    `control`, si fourni, est un dict mutable partagé avec l'appelant: tant
    que control.get("paused") est vrai, on arrête d'écrire de nouveaux blocs
    (la connexion HTTP reste ouverte) jusqu'à ce qu'une autre partie du code
    remette "paused" à False. Une pause trop longue peut faire fermer la
    connexion côté hébergeur; dans ce cas l'erreur remonte normalement et
    l'utilisateur peut relancer le téléchargement comme pour toute erreur."""
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer

    with requests.get(url, headers=headers, stream=True, timeout=30) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 1024 * 256
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as output_file:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                if control is not None:
                    while control.get("paused"):
                        time.sleep(0.3)
                output_file.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    on_progress(downloaded, total)
                elif total:
                    percent = downloaded * 100 // total
                    mb_done = downloaded / (1024 * 1024)
                    mb_total = total / (1024 * 1024)
                    print(
                        f"\r  {Colors.CYAN}{percent:3d}%{Colors.RESET} "
                        f"{mb_done:7.1f} / {mb_total:.1f} MB",
                        end="",
                        flush=True,
                    )
        if not on_progress:
            print()


def parse_episode_selection(raw, available_numbers):
    """Interpréter '1,3,5' ou '1-4' ou 'all' en une liste de numéros valides."""
    raw = raw.strip().lower()
    if raw in {"all", "tout", "tous", ""}:
        return sorted(available_numbers)

    selected = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            try:
                start_num, end_num = int(start), int(end)
            except ValueError:
                continue
            selected.update(range(start_num, end_num + 1))
        else:
            try:
                selected.add(int(part))
            except ValueError:
                continue
    return sorted(number for number in selected if number in available_numbers)


def display_results(episodes):
    """Afficher les épisodes extraits avec une présentation soignée."""
    if not episodes:
        print(f"{Colors.RED}Aucun épisode trouvé.{Colors.RESET}")
        return

    if not episodes["vf"] and not episodes["vostfr"]:
        print(f"\n{Colors.BOLD}{Colors.CYAN}SÉRIE:{Colors.RESET} {episodes['series_title']}")
        print(f"{Colors.RED}Aucun lien d'épisode n'est présent sur cette page.{Colors.RESET}")
        print(f"{Colors.DIM}Utilisez l'URL d'une page qui liste réellement les épisodes.{Colors.RESET}")
        return

    width = 60
    print()
    print(f"{Colors.BOLD}{Colors.MAGENTA}{'═' * width}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  SÉRIE: {episodes['series_title']}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.MAGENTA}{'═' * width}{Colors.RESET}")

    for language, label, color in (
        ("vf", "VERSION FRANÇAISE (VF)", Colors.GREEN),
        ("vostfr", "VERSION SOUS-TITRÉE (VOSTFR)", Colors.YELLOW),
    ):
        items = episodes[language]
        if not items:
            continue
        print(f"\n{Colors.BOLD}{color}▶ {label}{Colors.RESET} {Colors.DIM}({len(items)} épisode(s)){Colors.RESET}")
        print(f"{Colors.DIM}{'-' * width}{Colors.RESET}")
        for episode in items:
            print(f"  {color}Épisode {episode['number']:02d}{Colors.RESET}  {episode['title']}")

    total = len(episodes["vf"]) + len(episodes["vostfr"])
    print(f"\n{Colors.BOLD}{Colors.BLUE}TOTAL: {total} épisode(s){Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.MAGENTA}{'═' * width}{Colors.RESET}\n")


def save_to_json(episodes, filename=None):
    """Sauvegarder les métadonnées dans un fichier JSON."""
    if not filename:
        safe_title = re.sub(r"[^\w\s-]", "", episodes["series_title"]).strip()
        filename = f"{safe_title.replace(' ', '_') or 'episodes'}.json"
    with open(filename, "w", encoding="utf-8") as output_file:
        json.dump(episodes, output_file, ensure_ascii=False, indent=2)
    print(f"{Colors.GREEN}Sauvegardé dans: {filename}{Colors.RESET}")


def run_downloads(scraper, series_url, series_title, language, numbers):
    """Ouvrir un navigateur, résoudre puis télécharger chaque épisode choisi."""
    safe_title = re.sub(r"[^\w\s-]", "", series_title).strip().replace(" ", "_") or "serie"
    output_dir = os.path.join("downloads", safe_title, language)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=scraper.headless, args=LOW_MEMORY_CHROMIUM_ARGS)
        page = browser.new_page(user_agent=USER_AGENT)
        try:
            page.goto(series_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector(f"#{language}-episodes .episode-row", timeout=20000)

            for number in numbers:
                print(f"\n{Colors.BOLD}{Colors.CYAN}Épisode {number:02d} ({language.upper()}){Colors.RESET}")
                result = None
                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    print(f"  {Colors.DIM}Résolution du lien direct (essai {attempt}/{max_attempts})...{Colors.RESET}")
                    result = scraper.get_direct_download_url(page, language, number)
                    if result:
                        break
                    page.wait_for_timeout(1500)
                if not result:
                    print(f"  {Colors.RED}Impossible de récupérer le lien de téléchargement après {max_attempts} essais.{Colors.RESET}")
                    continue

                filename = f"{safe_title}_{language.upper()}_E{number:02d}.mp4"
                dest_path = os.path.join(output_dir, filename)
                print(f"  {Colors.GREEN}Lien trouvé{Colors.RESET} → téléchargement vers {dest_path}")
                try:
                    download_file(result["url"], dest_path, referer=result["referer"])
                    print(f"  {Colors.GREEN}Terminé: {dest_path}{Colors.RESET}")
                except requests.RequestException as error:
                    print(f"  {Colors.RED}Erreur de téléchargement: {error}{Colors.RESET}")
        finally:
            browser.close()


def run_downloads_anime_sama(series_url, series_title, language, numbers):
    """Équivalent de run_downloads pour anime-sama.to: pas de Playwright,
    juste des requêtes HTTP (via les résolveurs par hébergeur)."""
    from anime_sama import AnimeSamaScraper, resolve_episode_download

    safe_title = re.sub(r"[^\w\s-]", "", series_title).strip().replace(" ", "_") or "serie"
    output_dir = os.path.join("downloads", safe_title, language)

    episodes = AnimeSamaScraper().extract_episodes(series_url)
    episodes_by_number = {episode["number"]: episode for episode in episodes.get(language, [])}

    for number in numbers:
        print(f"\n{Colors.BOLD}{Colors.CYAN}Épisode {number:02d} ({language.upper()}){Colors.RESET}")
        episode = episodes_by_number.get(number)
        result = resolve_episode_download(episode["hosts"]) if episode else None
        if not result:
            print(f"  {Colors.RED}Aucun hébergeur supporté pour cet épisode (HLS uniquement ou non pris en charge).{Colors.RESET}")
            continue

        filename = f"{safe_title}_{language.upper()}_E{number:02d}.mp4"
        dest_path = os.path.join(output_dir, filename)
        print(f"  {Colors.GREEN}Lien trouvé{Colors.RESET} → téléchargement vers {dest_path}")
        try:
            download_file(result["url"], dest_path, referer=result["referer"])
            print(f"  {Colors.GREEN}Terminé: {dest_path}{Colors.RESET}")
        except requests.RequestException as error:
            print(f"  {Colors.RED}Erreur de téléchargement: {error}{Colors.RESET}")


def run_download_porno(url):
    """Télécharger une vidéo du site porno: extraction du flashvars (sans
    navigateur), choix de la qualité réellement disponible, puis téléchargement
    direct."""
    scraper = PornoScraper()
    print(f"\n{Colors.DIM}Extraction en cours (site porno)...{Colors.RESET}")
    info = scraper.extract_video(url)
    if not info:
        print(f"{Colors.RED}Impossible d'extraire la vidéo.{Colors.RESET}")
        return
    if not info["options"]:
        print(f"{Colors.RED}Aucune qualité de téléchargement trouvée.{Colors.RESET}")
        return

    width = 60
    print()
    print(f"{Colors.BOLD}{Colors.MAGENTA}{'═' * width}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  VIDÉO: {info['title']}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.MAGENTA}{'═' * width}{Colors.RESET}")
    if info.get("duration"):
        print(f"  {Colors.DIM}Durée: {info['duration']}{Colors.RESET}")
    print(f"\n{Colors.BOLD}{Colors.BLUE}Qualités disponibles:{Colors.RESET}")
    for index, option in enumerate(info["options"], start=1):
        marker = f" {Colors.DIM}(premium){Colors.RESET}" if option.get("premium") else ""
        print(f"  {Colors.GREEN}{index}{Colors.RESET}. {option['label']}{marker}")
    print()

    try:
        raw = input("Qualité à télécharger [1]: ").strip()
    except EOFError:
        return
    try:
        choice = int(raw) if raw else 1
    except ValueError:
        choice = 1
    if not 1 <= choice <= len(info["options"]):
        choice = 1
    option = info["options"][choice - 1]

    safe_title = re.sub(r"[^\w\s-]", "", info["title"]).strip().replace(" ", "_") or "video"
    safe_title = safe_title[:80]
    suffix = re.sub(r"[^\w]+", "_", option["label"]).strip("_")
    dest_path = os.path.join("downloads", "porno", f"{safe_title}_{suffix}.mp4")

    print(f"  {Colors.GREEN}Lien trouvé{Colors.RESET} → {dest_path}")
    try:
        if option["type"] == "hls":
            final_path = download_hls(option["url"], dest_path, referer=info["url"])
        else:
            download_file(option["url"], dest_path, referer=info["url"])
            final_path = dest_path
        print(f"  {Colors.GREEN}Terminé: {final_path}{Colors.RESET}")
    except Exception as error:
        print(f"  {Colors.RED}Erreur de téléchargement: {error}{Colors.RESET}")


def run_watch_football(url=None):
    """Menu CLI football: liste les matchs en direct, puis joue ou enregistre.

    Si `url` pointe déjà sur un match, la liste est sautée.
    FOOTBALL_DEBUG=1 active les traces d'extraction."""
    debug = os.environ.get("FOOTBALL_DEBUG") == "1"
    scraper = FootballScraper(debug=debug)

    # URL déjà sur un match (donc pas une page de listing).
    if url and "football.html" not in url and "live.html" not in url:
        match = {"title": url.rsplit("/", 1)[-1], "url": url}
    else:
        print(f"\n{Colors.DIM}Extraction des matchs de football en direct...{Colors.RESET}")
        matches = scraper.list_matches(only_live=True, limit=40)
        if not matches:
            print(f"{Colors.RED}Aucun match de football en direct pour le moment.{Colors.RESET}")
            print(f"{Colors.DIM}Tape 'footprobe' pour voir la structure réelle de la page, "
                  f"ou relance avec FOOTBALL_DEBUG=1 (ou tape 'footdebug').{Colors.RESET}")
            return

        width = 60
        print()
        print(f"{Colors.BOLD}{Colors.MAGENTA}{'═' * width}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.CYAN}  FOOTBALL EN DIRECT ({len(matches)}){Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.MAGENTA}{'═' * width}{Colors.RESET}")
        for index, item in enumerate(matches, start=1):
            badge = item["minute"] or item["score"] or "LIVE"
            print(f"  {Colors.YELLOW}{index:2d}{Colors.RESET}. {item['title']}  {Colors.GREEN}{badge}{Colors.RESET}")
            if item["league"]:
                print(f"      {Colors.DIM}{item['league']}{Colors.RESET}")
        print()

        try:
            raw = input("Numéro du match (Entrée pour quitter): ").strip()
        except EOFError:
            return
        if not raw.isdigit() or not 1 <= int(raw) <= len(matches):
            return
        match = matches[int(raw) - 1]

    print(f"\n{Colors.DIM}Résolution du flux direct...{Colors.RESET}")
    stream = scraper.get_stream(match)
    if not stream:
        print(f"{Colors.RED}Impossible de récupérer le flux (match terminé ou lecteur indisponible).{Colors.RESET}")
        return

    print(f"  {Colors.GREEN}Flux trouvé{Colors.RESET} → {stream['url']}")
    print(f"  {Colors.DIM}Colle l'URL dans VLC/MPV pour regarder en direct.{Colors.RESET}")

    try:
        choice = input("\n[v] VLC, [d] enregistrer, [Entrée] terminer: ").strip().lower()
    except EOFError:
        return
    if choice not in {"v", "d"}:
        return

    if choice == "v":
        webbrowser.open(stream["url"])
        return

    safe_title = re.sub(r"[^\w\s-]", "", match["title"]).strip().replace(" ", "_")[:80] or "match"
    dest_path = os.path.join("downloads", "football", f"{safe_title}.mp4")

    try:
        minutes = input("Durée d'enregistrement en minutes [90]: ").strip()
        seconds = int(minutes) * 60 if minutes.isdigit() else 90 * 60
    except EOFError:
        seconds = 90 * 60

    print(f"  {Colors.GREEN}Enregistrement {seconds // 60} min{Colors.RESET} → {dest_path}")
    try:
        final_path = record_live_hls(stream["url"], dest_path, referer=stream["referer"], seconds=seconds)
        print(f"  {Colors.GREEN}Terminé: {final_path}{Colors.RESET}")
    except Exception as error:
        print(f"  {Colors.RED}Erreur: {error}{Colors.RESET}")


def main():
    default_url = "https://french-stream.one/index.php?newsid=15131999"
    url = input(f"URL (série, film, vidéo, foot) [{default_url}]: ").strip() or default_url

    # Football: domaine du site, ou simple mot-clé "foot"/"football" pour
    # ouvrir directement la liste des matchs en direct.
    # "footdebug" lance le diagnostic d'extraction, "footprobe" affiche la
    # structure réelle des lignes détectées (utile si footdebug ne suffit pas
    # à comprendre pourquoi l'extraction remonte 0 ou un résultat bizarre).
    keyword = url.strip().lower()
    if keyword in {"footdebug", "foot-debug", "debugfoot"}:
        football_debug()
        return
    if keyword in {"footprobe", "probe"}:
        football_probe()
        return
    if any(domain in keyword for domain in FOOTBALL_DOMAINS):
        run_watch_football(url)
        return
    if keyword in {"foot", "football"}:
        run_watch_football()
        return

    if "pornhub" in url:
        run_download_porno(url)
        return

    is_anime_sama = "anime-sama.to" in url

    if is_anime_sama:
        from anime_sama import AnimeSamaScraper
        scraper = AnimeSamaScraper()
        print(f"\n{Colors.DIM}Extraction en cours (site anime-sama.to)...{Colors.RESET}")
    else:
        scraper = FrenchStreamScraper()
        print(f"\n{Colors.DIM}Extraction en cours (chargement du navigateur headless)...{Colors.RESET}")

    episodes = scraper.extract_episodes(url)
    if not episodes:
        print(f"{Colors.RED}Impossible d'extraire les épisodes.{Colors.RESET}")
        return

    display_results(episodes)
    if not episodes["vf"] and not episodes["vostfr"]:
        return

    try:
        save = input("Sauvegarder en JSON? (o/n): ").lower().strip()
    except EOFError:
        print(f"\n{Colors.RED}Entrée interrompue.{Colors.RESET}")
        return
    if save in {"o", "oui", "y", "yes"}:
        filename = input("Nom du fichier (défaut automatique): ").strip() or None
        save_to_json(episodes, filename)

    try:
        do_download = input("\nTélécharger des épisodes en vidéo HD? (o/n): ").lower().strip()
    except EOFError:
        return
    if do_download not in {"o", "oui", "y", "yes"}:
        return

    available = {"vf": episodes["vf"], "vostfr": episodes["vostfr"]}
    languages = [lang for lang, items in available.items() if items]
    if len(languages) > 1:
        lang_choice = input(f"Langue ({'/'.join(languages)}): ").strip().lower()
        language = lang_choice if lang_choice in languages else languages[0]
    else:
        language = languages[0]

    numbers_available = {ep["number"] for ep in available[language]}
    selection = input(
        f"Épisodes à télécharger (ex: 1,2,3 ou 1-6 ou 'all') [{min(numbers_available)}-{max(numbers_available)}]: "
    ).strip()
    numbers = parse_episode_selection(selection, numbers_available)
    if not numbers:
        print(f"{Colors.RED}Aucun épisode valide sélectionné.{Colors.RESET}")
        return

    print(f"\n{Colors.DIM}Téléchargement de {len(numbers)} épisode(s) en {language.upper()}...{Colors.RESET}")
    if is_anime_sama:
        run_downloads_anime_sama(url, episodes["series_title"], language, numbers)
    else:
        run_downloads(scraper, url, episodes["series_title"], language, numbers)


if __name__ == "__main__":
    main()