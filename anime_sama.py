import re
from html import unescape
from urllib.parse import urljoin, urlparse

import requests

from telecharger_episodes import USER_AGENT

SEARCH_RESULT_RE = re.compile(
    r'<a href="([^"]+)" class="asn-search-result"><img[^>]*src="([^"]*)"[^>]*/>'
    r'.*?<h3 class="asn-search-result-title">(.*?)</h3>'
    r'.*?<p class="asn-search-meta">([^<]*)</p>',
    re.DOTALL,
)
MARK_TAG_RE = re.compile(r"</?mark>")
SYNOPSIS_RE = re.compile(r'id="synopsisText"[^>]*>(.*?)</p>', re.DOTALL)
NOTE_RE = re.compile(r'<b>([\d,]+)</b>\s*<span class="oe-note-sur">\s*/10')
GENRE_PILL_RE = re.compile(r'class="genre-pill"[^>]*>([^<]+)</a>')

# anime-sama.to ne rend pas la liste des épisodes dans le DOM: chaque page
# saison/langue a un fichier JS voisin "episodes.js" qui contient un tableau
# par lecteur alternatif (eps1, eps2, ...). Le numéro qui suit "eps" n'a
# aucun rapport avec l'hébergeur (ça change selon les séries), donc on
# identifie chaque hébergeur par le nom de domaine trouvé dans les URLs,
# jamais par le numéro du tableau.
EPS_ARRAY_RE = re.compile(r"var\s+eps\d+\s*=\s*\[(.*?)\];", re.DOTALL)
URL_IN_ARRAY_RE = re.compile(r"['\"](https?://[^'\"]+)['\"]")

HOST_NAMES = {
    "sendvid.com": "sendvid",
    "video.sibnet.ru": "sibnet",
    "oneupload.to": "oneupload",
    "ansembed.net": "ansembed",
    "smoothpre.com": "smoothpre",
}


def _host_name(url):
    """Identifier l'hébergeur par domaine (insensible à la casse: anime-sama
    écrit parfois "Smoothpre.com" avec une majuscule)."""
    netloc = urlparse(url).netloc.lower()
    for domain, name in HOST_NAMES.items():
        if domain in netloc:
            return name
    return netloc or "inconnu"


def _resolve_sibnet(embed_url, browser=None):
    """Sibnet met le lien MP4 direct en clair dans le HTML (player.src([...])),
    pas besoin de JS. Le hotlinking exige le Referer de la page shell.php."""
    response = requests.get(embed_url, headers={"User-Agent": USER_AGENT}, timeout=20)
    response.raise_for_status()
    match = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', response.text)
    if not match:
        return None
    return {"url": urljoin(embed_url, match.group(1)), "referer": embed_url}


def _resolve_sendvid(embed_url, browser=None):
    """Sendvid embarque généralement <source src="...mp4"> directement dans
    le HTML de la page embed, sans exécution JS nécessaire."""
    response = requests.get(embed_url, headers={"User-Agent": USER_AGENT}, timeout=20)
    response.raise_for_status()
    match = re.search(r'<source[^>]+src="([^"]+\.mp4[^"]*)"', response.text)
    if not match:
        match = re.search(r'"(https?://[^"]+\.mp4[^"]*)"', response.text)
    if not match:
        return None
    return {"url": match.group(1), "referer": embed_url}


def _sniff_media_url(embed_url, browser, wait_ms=5000):
    """De plus en plus d'hébergeurs (uqload, ansembed, smoothpre, et des
    domaines-miroirs renommés dynamiquement) ne révèlent leur flux (.m3u8,
    parfois .mp4) qu'une fois le lecteur JS exécuté: le lien n'existe pas tel
    quel dans le HTML statique, il est généré/obfusqué. On ouvre la page dans
    un vrai navigateur et on intercepte la première requête réseau vers un
    flux vidéo plutôt que d'essayer de désobfusquer le JS du lecteur."""
    if browser is None:
        return None
    found = {}

    def on_request(request):
        request_url = request.url
        if "url" in found:
            return
        if ".m3u8" in request_url:
            found["url"] = request_url
            found["hls"] = True
        elif ".mp4" in request_url.split("?")[0]:
            found["url"] = request_url
            found["hls"] = False

    page = browser.new_page(user_agent=USER_AGENT, extra_http_headers={"Referer": "https://anime-sama.to/"})
    page.on("request", on_request)
    try:
        page.goto(embed_url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(wait_ms)
    except Exception:
        pass
    finally:
        page.close()

    if "url" not in found:
        return None
    return {"url": found["url"], "referer": embed_url, "hls": found["hls"]}


def _resolve_oneupload(embed_url, browser=None):
    """oneupload redirige vers un sous-domaine dynamique protégé par un
    challenge JS + injecte des popups publicitaires dans des iframes; on n'a
    pas trouvé de façon fiable d'en extraire un lien direct sans risquer de
    déclencher les pubs. Non supporté pour l'instant."""
    return None


# Dispatcher extensible: ajouter un nouvel hébergeur = ajouter une entrée ici
# (et dans HOST_NAMES si le nom de domaine n'y figure pas encore).
HOST_RESOLVERS = {
    "sibnet": _resolve_sibnet,
    "sendvid": _resolve_sendvid,
    "ansembed": _sniff_media_url,
    "smoothpre": _sniff_media_url,
    "uqload": _sniff_media_url,
    "oneupload": _resolve_oneupload,
}


def resolve_episode_download(hosts, browser=None):
    """Essayer chaque hébergeur d'un épisode dans l'ordre jusqu'à en trouver
    un qui donne un lien exploitable. Un hébergeur sans résolveur dédié est
    quand même tenté par interception réseau générique (si un navigateur est
    fourni) plutôt qu'ignoré d'office: ces sites renomment régulièrement leurs
    domaines-miroirs, les coder un par un serait sans fin."""
    for host in hosts:
        resolver = HOST_RESOLVERS.get(host["name"])
        if resolver is None and browser is not None:
            resolver = _sniff_media_url
        if not resolver:
            continue
        try:
            result = resolver(host["url"], browser)
        except Exception:
            result = None
        if result:
            return result
    return None


class AnimeSamaScraper:
    """Pas besoin de Playwright pour lister les épisodes d'une série connue:
    le titre est dans du HTML statique et episodes.js est un fichier JS
    statique, donc on utilise de simples requêtes HTTP. La recherche (search)
    fait exception: le site bloque les requêtes HTTP brutes vers son moteur
    de recherche AJAX, il faut donc un vrai navigateur (browser passé par
    l'appelant, comme pour FrenchStreamScraper)."""

    def __init__(self, headless=True):
        self.headless = headless  # gardé pour une interface commune avec FrenchStreamScraper

    def search(self, browser, query):
        """Interroger le moteur de recherche interne du site
        (/template-php/defaut/fetch.php) puis, pour chaque résultat, résoudre
        une page saison/langue réellement exploitable par extract_episodes:
        la page catalogue renvoyée par la recherche n'est qu'une page de
        présentation, les liens saison/langue sont injectés par le JS du
        site et n'existent pas dans le HTML statique."""
        page = browser.new_page(user_agent=USER_AGENT)
        try:
            page.goto("https://anime-sama.to/", wait_until="domcontentloaded", timeout=30000)
            raw = page.evaluate(
                """async (q) => {
                    const r = await fetch('/template-php/defaut/fetch.php', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: 'query=' + encodeURIComponent(q),
                    });
                    return await r.text();
                }""",
                query,
            )
            candidates = []
            for href, poster, title_html, meta in SEARCH_RESULT_RE.findall(raw or ""):
                if "Scans" in meta and "Anime" not in meta:
                    continue  # contenu manga uniquement: pas de vidéo à proposer ici
                title = unescape(MARK_TAG_RE.sub("", title_html)).strip()
                candidates.append({"title": title, "poster": poster or None, "catalogue_url": href})

            results = []
            for item in candidates[:6]:
                watch_url = self._resolve_watchable_url(page, item["catalogue_url"])
                if not watch_url:
                    continue
                results.append({
                    "site": "anime-sama",
                    "type": "serie",  # anime-sama.to n'est géré ici qu'en épisodes, jamais en film unique
                    "title": item["title"],
                    "poster": item["poster"],
                    "url": watch_url,
                })
            return results
        except Exception:
            return []
        finally:
            page.close()

    def _resolve_watchable_url(self, page, catalogue_url):
        """Depuis la page catalogue d'une série, trouver un vrai lien
        saison/langue cliquable (généré par le JS du site). Exclut les
        sections 'scan' (manga, pas de vidéo); préfère la saison 1 en VOSTFR
        si elle existe, sinon le premier lien saison/langue trouvé."""
        try:
            page.goto(catalogue_url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(600)
            links = page.eval_on_selector_all(
                "a[href*='/vostfr'], a[href*='/vf']",
                "els => els.map(e => e.getAttribute('href'))",
            )
        except Exception:
            return None
        candidates = [link for link in links if link and "/scan" not in link]
        if not candidates:
            return None

        def score(link):
            value = 10 if "/saison1/" in link else 0
            return value + (1 if "vostfr" in link else 0)

        candidates.sort(key=score, reverse=True)
        return urljoin(catalogue_url, candidates[0])

    def _series_metadata(self, url):
        """Titre + affiche + synopsis + note + genres de la série. anime-sama.to
        expose l'affiche via une balise <meta property="og:image"> et le
        synopsis/la note/les genres directement dans le HTML de la page
        racine du catalogue (.../catalogue/<slug>/), pas besoin de deviner/
        inventer ces informations."""
        # Le <h1> avec le vrai titre de la série n'est que sur la page racine
        # du catalogue, pas sur la page saison/langue.
        match = re.match(r"(https?://[^/]+/catalogue/[^/]+/)", url)
        root_url = match.group(1) if match else url
        info = {"title": None, "poster": None, "synopsis": None, "rating": None, "genres": None}
        try:
            response = requests.get(root_url, headers={"User-Agent": USER_AGENT}, timeout=20)
            response.raise_for_status()
            title_match = re.search(r"<h1[^>]*>([^<]+)</h1>", response.text)
            if title_match:
                info["title"] = title_match.group(1).strip()
            poster_match = re.search(r'<meta property="og:image" content="([^"]+)"', response.text)
            if poster_match:
                info["poster"] = poster_match.group(1)
            synopsis_match = SYNOPSIS_RE.search(response.text)
            if synopsis_match:
                info["synopsis"] = unescape(re.sub(r"\s+", " ", synopsis_match.group(1))).strip() or None
            note_match = NOTE_RE.search(response.text)
            if note_match:
                info["rating"] = float(note_match.group(1).replace(",", "."))
            genres = GENRE_PILL_RE.findall(response.text)
            if genres:
                info["genres"] = ", ".join(unescape(g).strip() for g in genres)
        except requests.RequestException:
            pass
        if info["title"]:
            return info
        # Repli: le <title> de la page saison contient aussi le nom de la série.
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            response.raise_for_status()
            title_match = re.search(r"<title>([^<|]+)", response.text)
            if title_match:
                info["title"] = title_match.group(1).strip(" -")
            if not info["poster"]:
                poster_match = re.search(r'<meta property="og:image" content="([^"]+)"', response.text)
                if poster_match:
                    info["poster"] = poster_match.group(1)
        except requests.RequestException:
            pass
        info["title"] = info["title"] or "Titre inconnu"
        return info

    @staticmethod
    def _language_variant_url(url, language):
        """URL de la même page saison dans l'autre langue (vf <-> vostfr)."""
        other = "vostfr" if language == "vf" else "vf"
        pattern = r"/vf(/|$)" if language == "vf" else r"/vostfr(/|$)"
        return re.sub(pattern, f"/{other}\\1", url)

    def _fetch_episodes_js(self, lang_url):
        """Récupérer et parser episodes.js pour UNE page saison/langue
        donnée. Renvoie une liste vide si cette langue n'existe pas pour
        cette série (404) ou si la page n'a pas pu être lue, plutôt que de
        faire échouer toute l'extraction pour l'autre langue."""
        episodes_js_url = urljoin(lang_url if lang_url.endswith("/") else lang_url + "/", "episodes.js")
        try:
            # Le pare-feu du site bloque (403) les requêtes vers ce fichier JS
            # statique sans Referer pointant vers la page saison/langue
            # elle-même (aucun souci sur les pages HTML normales, seulement
            # cet asset-ci).
            response = requests.get(
                episodes_js_url,
                headers={"User-Agent": USER_AGENT, "Referer": lang_url},
                timeout=20,
            )
            response.raise_for_status()
        except requests.RequestException:
            return []

        # Chaque tableau epsN correspond à un lecteur (hébergeur) alterné pour
        # la même liste d'épisodes: l'index dans le tableau = numéro d'épisode - 1.
        host_arrays = []
        for array_body in EPS_ARRAY_RE.findall(response.text):
            urls = URL_IN_ARRAY_RE.findall(array_body)
            if urls:
                host_arrays.append(urls)

        episode_count = max((len(arr) for arr in host_arrays), default=0)
        episodes = []
        seen_host_signatures = set()
        for index in range(episode_count):
            number = index + 1
            hosts = []
            for array in host_arrays:
                if index < len(array):
                    hosts.append({"name": _host_name(array[index]), "url": array[index]})

            # Le site liste parfois le même contenu sous deux numéros
            # différents (erreur d'indexation côté site): deux épisodes dont
            # tous les liens hébergeur sont identiques sont le même fichier,
            # jamais deux épisodes distincts qui partageraient par coïncidence
            # les mêmes liens (uniques par upload). On ne garde que la
            # première occurrence pour ne pas télécharger deux fois le même
            # contenu sous un faux numéro d'épisode.
            signature = tuple(sorted(host["url"] for host in hosts))
            if signature and signature in seen_host_signatures:
                continue
            seen_host_signatures.add(signature)

            episodes.append({
                "number": number,
                "title": f"Épisode {number}",
                "url": lang_url,
                "hosts": hosts,
            })
        return episodes

    def extract_episodes(self, url):
        """Toujours essayer les deux langues (VF et VOSTFR) pour la même
        série, peu importe laquelle des deux pages a été collée par
        l'utilisateur: le site les héberge sur deux pages séparées
        (.../vf/ et .../vostfr/), alors qu'on veut proposer le choix dans
        l'appli plutôt que forcer un second copier-coller d'URL."""
        is_vf_url = "/vf/" in url or url.rstrip("/").endswith("/vf")
        vf_url = url if is_vf_url else self._language_variant_url(url, "vf")
        vostfr_url = url if not is_vf_url else self._language_variant_url(url, "vostfr")

        meta = self._series_metadata(url)
        return {
            "series_title": meta["title"],
            "poster": meta["poster"],
            "synopsis": meta["synopsis"],
            "rating": meta["rating"],
            "genres": meta["genres"],
            "runtime": None,
            "url": url,
            "vf": self._fetch_episodes_js(vf_url),
            "vostfr": self._fetch_episodes_js(vostfr_url),
        }
