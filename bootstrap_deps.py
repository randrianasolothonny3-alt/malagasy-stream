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
    specs = [_requirement_spec(module) for module in missing]
    print(f"Modules manquants ({', '.join(missing)}), installation en cours...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", *specs], check=True)
    except subprocess.CalledProcessError:
        print(
            "Échec de l'installation automatique. Essayez manuellement : "
            f"{sys.executable} -m pip install {' '.join(specs)}"
        )
        raise
    importlib.invalidate_caches()
