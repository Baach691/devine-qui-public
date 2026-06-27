"""Alias manuels des membres (surnoms) + normalisation.

Sert au mode Hardcore : le joueur tape un nom librement (sans liste), et on
relie sa saisie au bon membre en tolérant fautes de frappe et surnoms.
Clé = nom affiché dans le jeu (global_name Discord), valeurs = surnoms.

⚠️ Les VRAIS surnoms ne sont PAS dans ce fichier : ils contiennent des pseudos
réels de membres. Ils sont chargés depuis `aliases.local.json` (NON versionné —
voir `aliases.example.json` pour le format). Fichier absent => aucun alias : le
mode Hardcore résout alors uniquement le nom affiché Discord (dégradation propre).
"""

import difflib
import json
import logging
import os
import unicodedata

log = logging.getLogger(__name__)

_LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aliases.local.json")


def _load_aliases() -> dict:
    """Charge les alias depuis aliases.local.json. Vide si absent/illisible."""
    try:
        with open(_LOCAL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log.info("aliases.local.json absent : aucun alias (mode Hardcore = nom affiché seul).")
        return {}
    except (OSError, ValueError) as e:
        log.warning("aliases.local.json illisible (%s) : aucun alias.", e)
        return {}
    if not isinstance(data, dict):
        return {}
    # On ignore les clés de commentaire (ex. "_comment") et on normalise les types.
    return {
        str(k): [str(x) for x in v]
        for k, v in data.items()
        if not k.startswith("_") and isinstance(v, list)
    }


ALIASES = _load_aliases()


def normalize(s: str) -> str:
    """minuscule, sans accents, alphanumérique seulement."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


# Index : nom canonique normalisé -> liste de surnoms.
_NORM_ALIASES = {normalize(k): v for k, v in ALIASES.items()}


def aliases_for_name(name: str) -> list:
    """Surnoms connus pour un nom affiché (match exact normalisé, sinon proche)."""
    nn = normalize(name)
    if nn in _NORM_ALIASES:
        return _NORM_ALIASES[nn]
    for canon_n, vals in _NORM_ALIASES.items():
        if difflib.SequenceMatcher(None, nn, canon_n).ratio() >= 0.86:
            return vals
    return []


def accepted_strings(name: str) -> list:
    """Toutes les graphies acceptées pour un membre : son nom + ses surnoms."""
    return [name] + aliases_for_name(name)


def similarity(guess: str, candidate: str) -> float:
    """Score 0..1 entre une saisie et une graphie acceptée (tolère les fautes)."""
    g, c = normalize(guess), normalize(candidate)
    if not g or not c:
        return 0.0
    if g == c:
        return 1.0
    # Contenance : une saisie incluse dans une graphie acceptée (ou l'inverse)
    # est considérée comme très proche (ex. un pseudo avec/sans ponctuation).
    if g in c or c in g:
        return max(0.9, difflib.SequenceMatcher(None, g, c).ratio())
    return difflib.SequenceMatcher(None, g, c).ratio()
