#!/usr/bin/env python3
"""Crée (ou répare) la commande Entry Point qui rend l'Activity LANÇABLE.

Symptôme corrigé : l'Activity apparaît dans la liste « Activités » mais n'a pas de
bouton « Lancer ». Cause : il manque la commande PRIMARY_ENTRY_POINT (type 4) avec
handler DISCORD_LAUNCH_ACTIVITY (2), qui relie l'app au lanceur d'activités.

Lancer depuis la racine du projet :
    .venv/bin/python scripts/create_entry_point.py

Utilise DISCORD_TOKEN + (VITE_)DISCORD_CLIENT_ID du .env. Idempotent : s'il en
existe déjà une, il la met à jour au lieu d'en créer une 2e (une seule autorisée).
"""

import json
import os
import sys
import urllib.error
import urllib.request

# Le script est dans scripts/ : on ajoute la racine du projet au chemin Python
# pour pouvoir importer config.py (sinon ModuleNotFoundError selon le dossier).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

APP_ID = config.DISCORD_CLIENT_ID
TOKEN = config.TOKEN

if not APP_ID:
    sys.exit("❌ VITE_DISCORD_CLIENT_ID (ou DISCORD_CLIENT_ID) absent du .env")
if not TOKEN:
    sys.exit("❌ DISCORD_TOKEN absent du .env")

API = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
# User-Agent OBLIGATOIRE : sans lui, Cloudflare bloque (erreur 1010). Discord
# impose le format "DiscordBot ($url, $version)".
HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "DiscordBot (https://github.com/Baach691/daily-guessr, 1.0)",
}

# Commande d'entrée. handler=2 → Discord lance l'Activity automatiquement.
# Nommée "jouer" pour ne pas entrer en conflit avec le /daily existant ; elle
# ajoute aussi une slash command /jouer qui ouvre l'Activity.
ENTRY = {
    "name": "jouer",
    "description": "Ouvrir le jeu Daily Guessr dans Discord",
    "type": 4,
    "handler": 2,
    "integration_types": [0],   # GUILD_INSTALL
    "contexts": [0, 1, 2],      # guilde, MP du bot, MP de groupe
}


def call(method: str, url: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8") or "null"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") or "null"
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def _app_id_from_token(tok: str):
    """Le 1er segment d'un token de bot est l'ID de l'application encodé en base64."""
    import base64
    try:
        seg = tok.split(".")[0]
        seg += "=" * (-len(seg) % 4)
        return base64.urlsafe_b64decode(seg).decode("ascii")
    except Exception:
        return None


def main():
    token_app = _app_id_from_token(TOKEN)
    print(f"App ID (VITE_DISCORD_CLIENT_ID) : {APP_ID}")
    print(f"App du DISCORD_TOKEN (bot)      : {token_app or '?'}")
    if token_app and str(token_app) != str(APP_ID):
        print("\n❌ MISMATCH : ton bot et ton Activity sont sur DEUX applications différentes.")
        print("   Le token du bot ne peut pas gérer les commandes de l'app de l'Activity.")
        print("   Deux options :")
        print("   1) (recommandé) Active les Activities sur l'app DU BOT, et mets SON")
        print(f"      Application ID ({token_app}) dans VITE_DISCORD_CLIENT_ID + son")
        print("      Client Secret dans DISCORD_CLIENT_SECRET. Puis relance ce script.")
        print("   2) Garde l'app séparée pour l'Activity, mais il faudra créer l'Entry")
        print("      Point avec les identifiants de CETTE app (pas le token du bot).")
        sys.exit(1)

    print()
    status, cmds = call("GET", API)
    if status != 200:
        sys.exit(f"❌ Impossible de lister les commandes ({status}): {cmds}")

    existing = next((c for c in cmds if c.get("type") == 4), None)
    if existing:
        print(f"ℹ️  Entry Point déjà présente (id={existing['id']}, "
              f"name={existing.get('name')}, handler={existing.get('handler')}). "
              f"Mise à jour…")
        status, out = call("PATCH", f"{API}/{existing['id']}", {
            "name": ENTRY["name"],
            "description": ENTRY["description"],
            "handler": ENTRY["handler"],
        })
    else:
        print("➕ Aucune Entry Point → création…")
        status, out = call("POST", API, ENTRY)

    if status in (200, 201):
        print(f"✅ OK ({status}). Commande : /{out.get('name')} (type {out.get('type')}, "
              f"handler {out.get('handler')}).")
        print("\n👉 Redémarre complètement Discord (Cmd+Q puis rouvre), rejoins un salon")
        print("   vocal de ton serveur de test, et relance l'Activity : le bouton")
        print("   « Lancer » doit apparaître (ou tape /jouer).")
    else:
        print(f"❌ Échec ({status}): {json.dumps(out, ensure_ascii=False)[:1000]}")


if __name__ == "__main__":
    main()
