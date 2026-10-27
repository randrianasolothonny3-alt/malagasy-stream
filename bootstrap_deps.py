"""Installe automatiquement les paquets Python manquants du projet, pour
qu'un clone fraîchement récupéré (autre PC, autre dépôt Git) fonctionne sans
commande pip manuelle.

N'installe que ce qui manque réellement (avec la version épinglée lue dans
requirements.txt), jamais le fichier entier d'un coup: `python app.py` n'a
besoin que de Flask/requests/Playwright/colorama, pas de gunicorn (serveur de
prod) ni pywebview (lanceur desktop uniquement). Installer le fichier entier
a déjà fait planter l'installation sur un PC Windows où le script .exe de
gunicorn, pourtant déjà présent et non concerné, était verrouillé par un
autre processus.
"""
import importlib.util
import os
import subprocess
import sys

_REQUIREMENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")

# Cas où le nom du module importé diffère du nom du paquet pip qui le fournit.
_PIP_NAME = {"webview": "pywebview"}


def _requirement_spec(module):
    """Reprendre la ligne exacte (avec sa version épinglée) de
    requirements.txt pour ce module, pour installer la même version que le
    reste du projet plutôt qu'un simple `pip install <nom>` qui prendrait la
    dernière version disponible."""
    pip_name = _PIP_NAME.get(module, module)
    try:
        with open(_REQUIREMENTS_PATH, encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if line.lower().startswith(pip_name.lower() + "=="):
                    return line
    except OSError:
        pass
    return pip_name


def ensure_installed(*modules):
    missing = [module for module in modules if importlib.util.find_spec(module) is None]
    if not missing:
        return

    # Un module à la fois: pip échoue parfois sur Windows en essayant de
    # réécrire le script .exe d'UN SEUL paquet (ex: "flask.exe -> .deleteme",
    # WinError 2 — bug connu de pip, souvent lié à un antivirus qui verrouille
    # brièvement le fichier qu'il vient d'écrire) alors que le paquet lui-même
    # a bien été copié dans site-packages. Grouper l'installation ferait
    # échouer tous les modules à cause d'un seul, et on ignorerait que l'import
    # fonctionne déjà malgré le code de sortie non nul de pip.
    still_missing = []
    for module in missing:
        spec = _requirement_spec(module)
        print(f"Module manquant ({module}), installation en cours...")
        subprocess.run([sys.executable, "-m", "pip", "install", spec])
        importlib.invalidate_caches()
        if importlib.util.find_spec(module) is None:
            still_missing.append((module, spec))

    if not still_missing:
        return

    specs_hint = "\n".join(f"  {sys.executable} -m pip install {spec}" for _, spec in still_missing)
    print(
        "Installation automatique incomplète pour: "
        + ", ".join(module for module, _ in still_missing)
        + ".\nSi pip signale une erreur Windows du type "
        "\"Failed to write executable\" / \"WinError 2 ... .deleteme\", "
        "mettez d'abord pip à jour puis réessayez:\n"
        f"  {sys.executable} -m pip install --upgrade pip\n"
        "Sinon, installez manuellement ce qui manque encore:\n" + specs_hint
    )
    raise RuntimeError(
        "Modules toujours manquants après installation: "
        + ", ".join(module for module, _ in still_missing)
    )
