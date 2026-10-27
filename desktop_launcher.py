"""Point d'entrée pour la version .exe de Malagasy-Stream.

Démarre le serveur Flask dans un thread en arrière-plan, puis ouvre une
véritable fenêtre desktop (pywebview) affichant l'interface. Fermer la
fenêtre arrête aussi le serveur.
"""
import os
import subprocess
import sys
import threading
import time

# Sur un clone fraîchement récupéré (autre PC), les paquets du projet ne sont
# pas encore installés. En .exe figé (PyInstaller), tout est déjà embarqué et
# sys.executable n'est de toute façon pas un interpréteur Python utilisable
# avec pip, donc on ne tente ça qu'en lancement depuis le code source.
if not getattr(sys, "frozen", False):
    from bootstrap_deps import ensure_installed

    ensure_installed("flask", "requests", "playwright", "colorama", "webview")

# En .exe (PyInstaller), le driver Playwright embarqué cherche les navigateurs
# à côté de lui-même (dans le dossier temporaire d'extraction) au lieu du
# cache habituel où `playwright install` les a réellement installés.
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    default_cache = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = default_cache

# Sur un PC où Chromium n'a jamais été téléchargé pour Playwright (aucun
# dossier chromium_headless_shell-* dans le cache ci-dessus), on l'installe
# automatiquement au premier lancement plutôt que de planter avec une erreur
# demandant à l'utilisateur de taper une commande dans un terminal.
def _chromium_deja_installe(cache_dir):
    if not os.path.isdir(cache_dir):
        return False
    return any(name.startswith("chromium") for name in os.listdir(cache_dir))


if not _chromium_deja_installe(os.environ["PLAYWRIGHT_BROWSERS_PATH"]):
    # On ne peut pas faire `sys.executable -m playwright install` : dans le
    # .exe figé, sys.executable est Malagasy-Stream.exe lui-même, pas un interpréteur
    # Python. On appelle donc directement le driver interne de Playwright,
    # comme le fait playwright.__main__, mais sans relancer l'application.
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    driver_executable, driver_cli = compute_driver_executable()
    subprocess.run(
        [driver_executable, driver_cli, "install", "chromium"],
        env=get_driver_env(),
        check=False,
    )

import webview

from app import app

PORT = 5000


def run_server():
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    time.sleep(1.2)  # laisse le serveur Flask démarrer avant d'ouvrir la fenêtre
    webview.create_window("Malagasy-Stream", f"http://127.0.0.1:{PORT}", width=900, height=750)
    webview.start()
