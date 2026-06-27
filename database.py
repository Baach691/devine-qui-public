"""Couche d'accès à la base SQLite (messages, opt-out, scores, daily, anti-répétition)."""

import json
import logging
import random
import sqlite3
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import config

log = logging.getLogger(__name__)

_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    author_name TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_guild   ON messages(guild_id);
CREATE INDEX IF NOT EXISTS idx_messages_author  ON messages(guild_id, author_id);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(guild_id, channel_id);

CREATE TABLE IF NOT EXISTS optout (
    user_id  INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS scores (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    correct  INTEGER NOT NULL DEFAULT 0,   -- nb de jours gagnés (pour ratio/streak)
    total    INTEGER NOT NULL DEFAULT 0,   -- nb de parties jouées
    points   INTEGER NOT NULL DEFAULT 0,   -- score classement (Normal +1, Hardcore +2)
    PRIMARY KEY (guild_id, user_id)
);

-- Verrou de difficulté : dès qu'un joueur clique "Jouer", sa difficulté est
-- figée pour ce jour (mode "qui a écrit ça ?" uniquement). Empêche de basculer
-- Hardcore <-> Normal sur le même Daily.
CREATE TABLE IF NOT EXISTS daily_lock (
    guild_id   INTEGER NOT NULL,
    date       TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    difficulty TEXT    NOT NULL,           -- 'normal' | 'hardcore'
    started_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, date, user_id)
);

-- Heure de départ serveur, PAR MODE, pour mesurer le temps de réponse de façon
-- non-truquable (le client ne peut pas envoyer un faux time_taken_ms). Le verrou
-- daily_lock ne couvre que "Qui a écrit ça ?" (author) ; cette table couvre les 3 modes.
CREATE TABLE IF NOT EXISTS daily_start (
    guild_id   INTEGER NOT NULL,
    date       TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    mode       TEXT    NOT NULL,
    started_at TEXT    NOT NULL,
    difficulty TEXT    NOT NULL DEFAULT 'normal',  -- 'normal' | 'hardcore', par mode
    PRIMARY KEY (guild_id, date, user_id, mode)
);

-- Anti-répétition : on garde la trace des messages récemment tirés.
CREATE TABLE IF NOT EXISTS recent_picks (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    picked_at  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_picks_recent ON recent_picks(guild_id, picked_at DESC);

-- Défi quotidien : un message par serveur par jour, identique pour tout le monde.
CREATE TABLE IF NOT EXISTS daily (
    guild_id    INTEGER NOT NULL,
    date        TEXT    NOT NULL,        -- YYYY-MM-DD (fuseau DAILY_TIMEZONE)
    message_id  INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    author_name TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    options     TEXT    NOT NULL,        -- JSON [[author_id, name], ...] (4 propositions)
    PRIMARY KEY (guild_id, date)
);

-- Une tentative par joueur par jour.
CREATE TABLE IF NOT EXISTS daily_attempts (
    guild_id      INTEGER NOT NULL,
    date          TEXT    NOT NULL,
    user_id       INTEGER NOT NULL,
    user_name     TEXT    NOT NULL,
    guessed_id    INTEGER NOT NULL,
    correct       INTEGER NOT NULL,
    answered_at   TEXT    NOT NULL,
    time_taken_ms INTEGER,
    PRIMARY KEY (guild_id, date, user_id)
);

-- Streaks par joueur : nb de jours consécutifs trouvés (current/best).
CREATE TABLE IF NOT EXISTS streaks (
    guild_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    current_streak    INTEGER NOT NULL DEFAULT 0,
    best_streak       INTEGER NOT NULL DEFAULT 0,
    last_correct_date TEXT,
    last_broken_streak INTEGER NOT NULL DEFAULT 0,
    last_broken_date TEXT,
    current_loss_streak INTEGER NOT NULL DEFAULT 0,
    last_loss_date TEXT,
    PRIMARY KEY (guild_id, user_id)
);

-- Profils utilisateurs (nom + avatar) pour l'affichage côté webapp.
CREATE TABLE IF NOT EXISTS users (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    avatar_url TEXT,
    PRIMARY KEY (guild_id, user_id)
);

-- Annonces déjà envoyées : garantit qu'on annonce le daily au plus une fois/jour,
-- quelle que soit la voie qui a créé le défi (cron, boot, ou premier /daily).
CREATE TABLE IF NOT EXISTS daily_announced (
    guild_id     INTEGER NOT NULL,
    date         TEXT    NOT NULL,
    announced_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, date)
);

-- =====================================================================
-- MODE "devine la phrase" : tables séparées, parfaitement symétriques.
-- On tire un utilisateur cible, et 4 phrases (1 de lui + 3 d'autres) :
-- deviner laquelle il a écrite. Streaks/scores/classement indépendants.
-- =====================================================================

CREATE TABLE IF NOT EXISTS phrase_daily (
    guild_id           INTEGER NOT NULL,
    date               TEXT    NOT NULL,
    target_author_id   INTEGER NOT NULL,   -- l'utilisateur cible
    target_author_name TEXT    NOT NULL,
    correct_message_id INTEGER NOT NULL,   -- la phrase qu'il a écrite (bonne réponse)
    channel_id         INTEGER NOT NULL,   -- salon de la bonne phrase (pour le contexte)
    content            TEXT    NOT NULL,   -- texte de la bonne phrase
    options            TEXT    NOT NULL,   -- JSON [[message_id, texte], ...] (4 propositions)
    PRIMARY KEY (guild_id, date)
);

CREATE TABLE IF NOT EXISTS phrase_daily_attempts (
    guild_id      INTEGER NOT NULL,
    date          TEXT    NOT NULL,
    user_id       INTEGER NOT NULL,
    user_name     TEXT    NOT NULL,
    guessed_id    INTEGER NOT NULL,
    correct       INTEGER NOT NULL,
    answered_at   TEXT    NOT NULL,
    time_taken_ms INTEGER,
    PRIMARY KEY (guild_id, date, user_id)
);

CREATE TABLE IF NOT EXISTS phrase_streaks (
    guild_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    current_streak    INTEGER NOT NULL DEFAULT 0,
    best_streak       INTEGER NOT NULL DEFAULT 0,
    last_correct_date TEXT,
    last_broken_streak INTEGER NOT NULL DEFAULT 0,
    last_broken_date TEXT,
    current_loss_streak INTEGER NOT NULL DEFAULT 0,
    last_loss_date TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS phrase_scores (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    correct  INTEGER NOT NULL DEFAULT 0,
    total    INTEGER NOT NULL DEFAULT 0,
    points   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS phrase_recent_picks (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    picked_at  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_phrase_picks ON phrase_recent_picks(guild_id, picked_at DESC);

CREATE TABLE IF NOT EXISTS phrase_daily_announced (
    guild_id     INTEGER NOT NULL,
    date         TEXT    NOT NULL,
    announced_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, date)
);

-- =====================================================================
-- MODE "devine le média" (annexe, hors classement principal pour l'instant).
-- Identique au mode auteur (deviner qui a envoyé), mais le défi affiche un
-- média (image/gif/vidéo). Tables media_* : toutes les stats sont enregistrées
-- comme si le mode participait au classement, pour une intégration future.
-- =====================================================================

CREATE TABLE IF NOT EXISTS media_daily (
    guild_id    INTEGER NOT NULL,
    date        TEXT    NOT NULL,
    message_id  INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    author_name TEXT    NOT NULL,
    content     TEXT    NOT NULL,        -- URL du média
    options     TEXT    NOT NULL,        -- JSON [[author_id, name], ...]
    PRIMARY KEY (guild_id, date)
);

CREATE TABLE IF NOT EXISTS media_daily_attempts (
    guild_id      INTEGER NOT NULL,
    date          TEXT    NOT NULL,
    user_id       INTEGER NOT NULL,
    user_name     TEXT    NOT NULL,
    guessed_id    INTEGER NOT NULL,
    correct       INTEGER NOT NULL,
    answered_at   TEXT    NOT NULL,
    time_taken_ms INTEGER,
    difficulty    TEXT    NOT NULL DEFAULT 'normal',
    PRIMARY KEY (guild_id, date, user_id)
);

CREATE TABLE IF NOT EXISTS media_streaks (
    guild_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    current_streak    INTEGER NOT NULL DEFAULT 0,
    best_streak       INTEGER NOT NULL DEFAULT 0,
    last_correct_date TEXT,
    last_broken_streak INTEGER NOT NULL DEFAULT 0,
    last_broken_date TEXT,
    current_loss_streak INTEGER NOT NULL DEFAULT 0,
    last_loss_date TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS media_scores (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    correct  INTEGER NOT NULL DEFAULT 0,
    total    INTEGER NOT NULL DEFAULT 0,
    points   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS media_recent_picks (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    picked_at  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_media_picks ON media_recent_picks(guild_id, picked_at DESC);

CREATE TABLE IF NOT EXISTS media_daily_announced (
    guild_id     INTEGER NOT NULL,
    date         TEXT    NOT NULL,
    announced_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, date)
);
"""


def get_conn() -> sqlite3.Connection:
    """Connexion unique réutilisée (partagée entre le bot async et Flask)."""
    global _conn
    if _conn is None:
        # check_same_thread=False car Flask tourne dans un autre thread que discord.py.
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        # WAL : lecteurs concurrents + 1 écrivain sans se bloquer mutuellement.
        # busy_timeout : on attend (5 s) au lieu de lever "database is locked".
        try:
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=5000")
            _conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
    return _conn


# Modes de jeu. "author" = identifier l'auteur (tables historiques sans préfixe).
# "phrase" = devine quelle phrase l'utilisateur a écrite (tables phrase_*).
MODE_AUTHOR = "author"
MODE_PHRASE = "phrase"
MODE_MEDIA = "media"
VALID_MODES = (MODE_AUTHOR, MODE_PHRASE, MODE_MEDIA)


def _tbl(mode: str, base: str) -> str:
    """Nom de table physique pour un mode donné.

    mode='author' → tables historiques (`scores`, `streaks`, ...).
    mode='phrase' → tables préfixées (`phrase_scores`, ...).
    `base` est un littéral interne (jamais une entrée utilisateur) → f-string sûr.
    """
    if mode == MODE_AUTHOR:
        return base
    if mode in (MODE_PHRASE, MODE_MEDIA):
        return f"{mode}_{base}"
    raise ValueError(f"mode inconnu: {mode!r}")


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    # Migrations idempotentes pour les BDD déjà existantes.
    _MIGRATIONS = [
        "ALTER TABLE daily_attempts ADD COLUMN time_taken_ms INTEGER",
        "ALTER TABLE streaks ADD COLUMN last_broken_streak INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE streaks ADD COLUMN last_broken_date TEXT",
        "ALTER TABLE streaks ADD COLUMN current_loss_streak INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE streaks ADD COLUMN last_loss_date TEXT",
        # Hardcore : score en points + difficulté jouée.
        "ALTER TABLE scores ADD COLUMN points INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE phrase_scores ADD COLUMN points INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE daily_attempts ADD COLUMN difficulty TEXT NOT NULL DEFAULT 'normal'",
        "ALTER TABLE phrase_daily_attempts ADD COLUMN difficulty TEXT NOT NULL DEFAULT 'normal'",
        # Hardcore par mode (author + média) : la difficulté est portée par
        # daily_start (mode-aware), pas par daily_lock (qui ne couvrait que author).
        "ALTER TABLE daily_start ADD COLUMN difficulty TEXT NOT NULL DEFAULT 'normal'",
    ]
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as err:
            if "duplicate column" not in str(err).lower():
                raise

    # Backfill points = victoires pour les joueurs existants : toutes les parties
    # passées étaient en Normal (+1), donc points == correct. On ne backfill que
    # les lignes encore "vierges" (points=0 alors qu'il y a des victoires), sans
    # écraser de vrais points déjà cumulés en Hardcore.
    for sc in ("scores", "phrase_scores", "media_scores"):
        conn.execute(
            f"UPDATE {sc} SET points = correct WHERE points = 0 AND correct > 0"
        )
    conn.commit()

    # Reconstruit les séries de défaites depuis l'historique (pour les joueurs
    # existants d'avant l'ajout de la feature). Idempotent : recalculé depuis
    # daily_attempts, identique à ce que produit update_streak au fil de l'eau.
    for m in VALID_MODES:
        recompute_loss_streaks(mode=m)

    # Auto-réparation des tentatives marquées fausses à tort (ancien bug : perte
    # de précision JS sur les snowflakes Discord → la bonne réponse comptait faux).
    # Idempotent : ne remet correct=1 que si l'id arrondi correspond à la bonne
    # option. Si quelque chose a été corrigé, on rejoue scores + streaks pour que
    # le classement reflète les vrais résultats. Au démarrage suivant : no-op.
    try:
        fixed = repair_precision_attempts()
        if fixed:
            log.warning(
                "Réparation précision : %d tentative(s) recomptée(s) comme bonne(s). "
                "Recalcul des scores et séries.", fixed,
            )
            for m in VALID_MODES:
                recompute_player_stats(mode=m)
    except Exception:
        log.exception("Échec de la réparation des tentatives de précision (ignoré).")


def recompute_loss_streaks(mode: str = MODE_AUTHOR) -> None:
    """Reconstruit current_loss_streak + last_loss_date depuis daily_attempts.

    Ne touche pas aux séries de victoires (current_streak / best_streak).
    Applique la même règle que update_streak : la série de défaites chaîne
    uniquement sur des jours calendaires consécutifs.
    """
    from itertools import groupby

    da_t = _tbl(mode, "daily_attempts")
    st_t = _tbl(mode, "streaks")
    conn = get_conn()
    rows = conn.execute(
        f"SELECT guild_id, user_id, date, correct FROM {da_t} "
        "ORDER BY guild_id, user_id, date ASC"
    ).fetchall()

    for (gid, uid), attempts in groupby(rows, key=lambda r: (r["guild_id"], r["user_id"])):
        loss_streak = 0
        last_loss_date = None
        for a in attempts:
            if a["correct"]:
                loss_streak = 0
            else:
                prev_day = (date.fromisoformat(a["date"]) - timedelta(days=1)).isoformat()
                loss_streak = loss_streak + 1 if last_loss_date == prev_day else 1
                last_loss_date = a["date"]

        cur = conn.execute(
            f"UPDATE {st_t} SET current_loss_streak = ?, last_loss_date = ? "
            "WHERE guild_id = ? AND user_id = ?",
            (loss_streak, last_loss_date, gid, uid),
        )
        if cur.rowcount == 0:
            # Aucune ligne streaks (rare) : on la crée avec des valeurs neutres.
            conn.execute(
                f"INSERT OR IGNORE INTO {st_t} "
                "(guild_id, user_id, current_streak, best_streak, last_correct_date, "
                " last_broken_streak, last_broken_date, current_loss_streak, last_loss_date) "
                "VALUES (?, ?, 0, 0, NULL, 0, NULL, ?, ?)",
                (gid, uid, loss_streak, last_loss_date),
            )
    conn.commit()


def _recompute_player_stats_in_transaction(
    conn: sqlite3.Connection, mode: str
) -> None:
    """Reconstruit scores + streaks dans la transaction SQLite courante."""
    from itertools import groupby

    da_t = _tbl(mode, "daily_attempts")
    sc_t = _tbl(mode, "scores")
    st_t = _tbl(mode, "streaks")
    rows = conn.execute(
        f"SELECT guild_id, user_id, user_name, date, correct, difficulty FROM {da_t} "
        "ORDER BY guild_id, user_id, date ASC"
    ).fetchall()

    # On calcule TOUT en mémoire d'abord, puis on remplace dans UNE transaction
    # atomique (BEGIN IMMEDIATE) → pas de fenêtre où le classement serait vide.
    score_rows = []
    streak_rows = []
    for (gid, uid), attempts in groupby(rows, key=lambda r: (r["guild_id"], r["user_id"])):
        attempts = list(attempts)
        name = attempts[-1]["user_name"]
        correct = total = points = 0
        cur = best = loss = lbs = 0
        last_correct = last_loss = lbd = None
        for a in attempts:
            d = date.fromisoformat(a["date"])
            yest = (d - timedelta(days=1)).isoformat()
            total += 1
            if a["correct"]:
                correct += 1
                points += 2 if a["difficulty"] == "hardcore" else 1
                cur = cur + 1 if last_correct == yest else 1
                last_correct = a["date"]
                loss = 0
            else:
                active = cur > 0 and last_correct == yest
                lbs = cur if active else 0
                lbd = a["date"] if lbs > 0 else lbd
                cur = 0
                loss = loss + 1 if last_loss == yest else 1
                last_loss = a["date"]
            best = max(best, cur)
        score_rows.append((gid, uid, name, correct, total, points))
        streak_rows.append((gid, uid, cur, best, last_correct, lbs, lbd, loss, last_loss))

    conn.execute(f"DELETE FROM {sc_t}")
    conn.execute(f"DELETE FROM {st_t}")
    conn.executemany(
        f"INSERT INTO {sc_t} (guild_id, user_id, name, correct, total, points) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        score_rows,
    )
    conn.executemany(
        f"INSERT INTO {st_t} (guild_id, user_id, current_streak, best_streak, "
        " last_correct_date, last_broken_streak, last_broken_date, "
        " current_loss_streak, last_loss_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        streak_rows,
    )


def recompute_player_stats(mode: str = MODE_AUTHOR) -> None:
    """Reconstruit ENTIÈREMENT scores + streaks depuis daily_attempts (replay).

    daily_attempts est la source de vérité (une ligne par tentative). On rejoue
    l'historique pour recalculer correct/total/points et toutes les séries, en
    appliquant exactement la même logique que record_answer + update_streak.
    Idempotent : à utiliser après une correction de tentatives.
    """
    # Connexion dédiée : le replay ne partage pas sa transaction avec le thread
    # du bot ou une autre requête Flask utilisant la connexion globale.
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        _recompute_player_stats_in_transaction(conn, mode)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_daily_dates(guild_id: int) -> List[str]:
    """Dates possédant au moins une tentative, tous modes confondus."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT date FROM daily_attempts WHERE guild_id = ?
        UNION
        SELECT date FROM phrase_daily_attempts WHERE guild_id = ?
        UNION
        SELECT date FROM media_daily_attempts WHERE guild_id = ?
        ORDER BY date DESC
        """,
        (guild_id, guild_id, guild_id),
    ).fetchall()
    return [row["date"] for row in rows]


def correct_daily_attempt(
    guild_id: int,
    date_str: str,
    user_id: int,
    guessed_id: int,
    mode: str = MODE_AUTHOR,
) -> Optional[Dict]:
    """Corrige une réponse puis rejoue tous les agrégats dans une transaction.

    La tentative est la source de vérité. Sa difficulté et son temps sont
    conservés ; seuls la réponse choisie et le résultat qui en découle changent.
    Renvoie l'ancienne et la nouvelle valeur, ou None si la tentative/le défi
    n'existe pas.
    """
    specs = {
        MODE_AUTHOR: ("daily", "author_id"),
        MODE_PHRASE: ("phrase_daily", "correct_message_id"),
        MODE_MEDIA: ("media_daily", "author_id"),
    }
    challenge_table, correct_column = specs.get(mode, (None, None))
    if challenge_table is None:
        raise ValueError(f"mode inconnu: {mode!r}")

    attempt_table = _tbl(mode, "daily_attempts")
    # Connexion dédiée : la correction ne partage pas sa transaction avec le
    # thread du bot ou une autre requête Flask utilisant la connexion globale.
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        attempt = conn.execute(
            f"SELECT guessed_id, correct FROM {attempt_table} "
            "WHERE guild_id = ? AND date = ? AND user_id = ?",
            (guild_id, date_str, user_id),
        ).fetchone()
        challenge = conn.execute(
            f"SELECT {correct_column} AS correct_id FROM {challenge_table} "
            "WHERE guild_id = ? AND date = ?",
            (guild_id, date_str),
        ).fetchone()
        if attempt is None or challenge is None:
            conn.rollback()
            return None

        is_correct = int(guessed_id == challenge["correct_id"])
        conn.execute(
            f"UPDATE {attempt_table} SET guessed_id = ?, correct = ? "
            "WHERE guild_id = ? AND date = ? AND user_id = ?",
            (guessed_id, is_correct, guild_id, date_str, user_id),
        )
        _recompute_player_stats_in_transaction(conn, mode)
        conn.commit()
        return {
            "old_guessed_id": attempt["guessed_id"],
            "old_correct": bool(attempt["correct"]),
            "guessed_id": guessed_id,
            "correct": bool(is_correct),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def repair_precision_attempts() -> int:
    """Corrige les tentatives où la bonne réponse a été marquée fausse à cause
    de la perte de précision JS sur les snowflakes Discord.

    Pour chaque défi, si guessed_id == arrondi-JS(bonne réponse) alors le joueur
    avait bien cliqué la bonne option : on remet correct=1. Renvoie le nb corrigé.
    NE recalcule PAS scores/streaks (appeler recompute_player_stats ensuite).
    """
    conn = get_conn()
    fixed = 0
    # (table du défi, colonne de la bonne réponse, mode)
    specs = [
        ("daily", "author_id", MODE_AUTHOR),
        ("media_daily", "author_id", MODE_MEDIA),
        ("phrase_daily", "correct_message_id", MODE_PHRASE),
    ]
    for daily_tbl, col, mode in specs:
        da_t = _tbl(mode, "daily_attempts")
        dailies = conn.execute(
            f"SELECT guild_id, date, {col} AS cid FROM {daily_tbl}"
        ).fetchall()
        for row in dailies:
            cid = row["cid"]
            rounded = int(float(cid))  # ce que JS aurait renvoyé en cliquant la bonne option
            if rounded == cid:
                continue  # pas de perte de précision sur cet id
            cur = conn.execute(
                f"UPDATE {da_t} SET correct = 1 "
                "WHERE guild_id = ? AND date = ? AND correct = 0 AND guessed_id = ?",
                (row["guild_id"], row["date"], rounded),
            )
            fixed += cur.rowcount
    conn.commit()
    return fixed


# --- Messages -------------------------------------------------------------

def add_message(
    message_id: int,
    guild_id: int,
    channel_id: int,
    author_id: int,
    author_name: str,
    content: str,
    created_at: str,
) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(message_id, guild_id, channel_id, author_id, author_name, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (message_id, guild_id, channel_id, author_id, author_name, content, created_at),
    )
    conn.commit()


def add_messages_bulk(rows: List[Tuple]) -> int:
    """Insère plusieurs messages d'un coup. Renvoie le nb de nouvelles lignes."""
    if not rows:
        return 0
    conn = get_conn()
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO messages "
        "(message_id, guild_id, channel_id, author_id, author_name, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def _apply_config_filters(sql: List[str], params: List) -> None:
    """Ajoute les clauses ALLOWED_CHANNEL_IDS, BLACKLIST_USER_IDS et l'exclusion
    des comptes supprimés ("Deleted User") depuis la config."""
    allowed = getattr(config, "ALLOWED_CHANNEL_IDS", []) or []
    if allowed:
        placeholders = ",".join("?" * len(allowed))
        sql.append(f"  AND channel_id IN ({placeholders})")
        params.extend(allowed)
    blacklist = getattr(config, "BLACKLIST_USER_IDS", []) or []
    if blacklist:
        placeholders = ",".join("?" * len(blacklist))
        sql.append(f"  AND author_id NOT IN ({placeholders})")
        params.extend(blacklist)
    # Exclut les comptes supprimés déjà stockés en base (vieux messages).
    sql.append(
        "  AND LOWER(author_name) NOT LIKE 'deleted\\_user%' ESCAPE '\\'"
        "  AND LOWER(author_name) != 'deleted user'"
    )


def _build_random_query(
    guild_id: int,
    channel_id: Optional[int],
    exclude_recent: int,
    date_range: Optional[Tuple[str, str]] = None,
):
    """Construit (sql, params) pour un tirage aléatoire avec filtres optionnels.

    `date_range` est un couple ('YYYY-MM-DD', 'YYYY-MM-DD'), borne haute exclusive.
    """
    sql = [
        "SELECT message_id, channel_id, author_id, author_name, content, created_at",
        "FROM messages",
        "WHERE guild_id = ?",
        "  AND author_id NOT IN (SELECT user_id FROM optout WHERE guild_id = ?)",
    ]
    params: List = [guild_id, guild_id]
    if channel_id is not None:
        sql.append("  AND channel_id = ?")
        params.append(channel_id)
    _apply_config_filters(sql, params)
    if date_range is not None:
        low, high = date_range
        sql.append("  AND created_at >= ? AND created_at < ?")
        params.extend([low, high])
    if exclude_recent > 0:
        sql.append(
            "  AND message_id NOT IN ("
            "    SELECT message_id FROM recent_picks "
            "    WHERE guild_id = ? ORDER BY picked_at DESC LIMIT ?"
            "  )"
        )
        params.extend([guild_id, exclude_recent])
    sql.append("ORDER BY RANDOM() LIMIT 1")
    return "\n".join(sql), params


def _resolve_oldest_date() -> date:
    """Lit la borne basse depuis la config, avec fallback sûr."""
    raw = getattr(config, "OLDEST_MESSAGE_DATE", None)
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return date(2023, 10, 1)


def get_random_message(
    guild_id: int,
    channel_id: Optional[int] = None,
    exclude_recent: Optional[int] = None,
) -> Optional[Tuple]:
    """Tire un message au hasard, en priorisant la diversité temporelle.

    Stratégie : choisit une date au hasard entre OLDEST_MESSAGE_DATE et aujourd'hui,
    puis cherche un message dans une fenêtre autour. Si rien dans la fenêtre,
    on l'élargit (3, 15, 61, 181 jours). En dernier recours, tirage global sans
    contrainte de date. Si l'anti-répétition vide le résultat, on retombe sur
    un tirage sans exclusion (mieux que ne rien proposer).

    Renvoie (message_id, channel_id, author_id, author_name, content, created_at)
    ou None s'il n'y a vraiment aucun message en base.
    """
    if exclude_recent is None:
        exclude_recent = config.RECENT_PICKS_EXCLUDE

    conn = get_conn()
    start = _resolve_oldest_date()
    end = date.today()

    if end > start:
        span_days = (end - start).days
        # On essaie quelques tirages avec des fenêtres de plus en plus larges.
        for window in (1, 7, 30, 90):
            offset = random.randint(0, span_days)
            target = start + timedelta(days=offset)
            low_iso = (target - timedelta(days=window)).isoformat()
            high_iso = (target + timedelta(days=window + 1)).isoformat()
            sql, params = _build_random_query(
                guild_id, channel_id, exclude_recent, date_range=(low_iso, high_iso)
            )
            row = conn.execute(sql, params).fetchone()
            if row:
                return tuple(row)

    # Fallback : tirage global (sans contrainte de date).
    sql, params = _build_random_query(guild_id, channel_id, exclude_recent)
    row = conn.execute(sql, params).fetchone()
    if row is None and exclude_recent > 0:
        # Dernier recours : on lève aussi l'anti-répétition.
        sql, params = _build_random_query(guild_id, channel_id, 0)
        row = conn.execute(sql, params).fetchone()
    return tuple(row) if row else None


def get_distinct_authors(
    guild_id: int,
    exclude_id: int,
    limit: int,
    channel_id: Optional[int] = None,
) -> List[Tuple[int, str]]:
    """Tire `limit` auteurs distincts au hasard (pour les mauvaises réponses).

    Applique aussi ALLOWED_CHANNEL_IDS et BLACKLIST_USER_IDS : pas de blacklisté
    comme distractor, et restriction aux salons whitelistés si configurée.
    """
    sql = [
        "SELECT author_id, author_name FROM messages",
        "WHERE guild_id = ?",
        "  AND author_id != ?",
        "  AND author_id NOT IN (SELECT user_id FROM optout WHERE guild_id = ?)",
    ]
    params: List = [guild_id, exclude_id, guild_id]
    if channel_id is not None:
        sql.append("  AND channel_id = ?")
        params.append(channel_id)
    _apply_config_filters(sql, params)
    sql.append("GROUP BY author_id ORDER BY RANDOM() LIMIT ?")
    params.append(limit)

    rows = get_conn().execute("\n".join(sql), params).fetchall()
    return [(r["author_id"], r["author_name"]) for r in rows]


def count_messages(guild_id: int, channel_id: Optional[int] = None) -> int:
    conn = get_conn()
    if channel_id is None:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ?", (guild_id,)
        ).fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchone()[0]


def count_authors(guild_id: int) -> int:
    conn = get_conn()
    return conn.execute(
        "SELECT COUNT(DISTINCT author_id) FROM messages WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()[0]


# --- Anti-répétition ------------------------------------------------------

def record_pick(guild_id: int, message_id: int, mode: str = MODE_AUTHOR) -> None:
    """Marque un message comme "récemment tiré" (anti-répétition, par mode)."""
    tbl = _tbl(mode, "recent_picks")
    conn = get_conn()
    conn.execute(
        f"INSERT OR REPLACE INTO {tbl} (guild_id, message_id, picked_at) "
        "VALUES (?, ?, datetime('now'))",
        (guild_id, message_id),
    )
    # On garde un cache borné : au-delà de 500 entrées par serveur on purge les plus vieilles.
    conn.execute(
        f"DELETE FROM {tbl} WHERE guild_id = ? AND message_id NOT IN ("
        f"  SELECT message_id FROM {tbl} "
        "  WHERE guild_id = ? ORDER BY picked_at DESC LIMIT 500"
        ")",
        (guild_id, guild_id),
    )
    conn.commit()


def get_recent_picks_set(guild_id: int, limit: int = 100, mode: str = MODE_AUTHOR) -> set:
    """Renvoie l'ensemble des `limit` derniers message_ids tirés (pour filtrer en mémoire)."""
    tbl = _tbl(mode, "recent_picks")
    rows = get_conn().execute(
        f"SELECT message_id FROM {tbl} WHERE guild_id = ? "
        "ORDER BY picked_at DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    return {r["message_id"] for r in rows}


# --- Opt-out (respect de la vie privée) -----------------------------------

def is_opted_out(user_id: int, guild_id: int) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM optout WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    ).fetchone()
    return row is not None


def opt_out(user_id: int, guild_id: int) -> int:
    """Exclut un utilisateur du jeu et supprime ses messages déjà stockés.

    Renvoie le nombre de messages supprimés.
    """
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM messages WHERE author_id = ? AND guild_id = ?",
        (user_id, guild_id),
    )
    deleted = cur.rowcount
    conn.execute(
        "INSERT OR IGNORE INTO optout (user_id, guild_id) VALUES (?, ?)",
        (user_id, guild_id),
    )
    conn.commit()
    return deleted


def opt_in(user_id: int, guild_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "DELETE FROM optout WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    )
    conn.commit()


# --- Verrou de difficulté (Hardcore) --------------------------------------
# La difficulté + l'heure de départ sont désormais portées par daily_start
# (mode-aware : Hardcore sur "Qui a écrit ça ?" ET "Devine le média"). L'ancienne table
# daily_lock (author-only) n'est plus utilisée ; on la garde en place pour ne pas
# casser les BDD existantes, mais plus aucun code ne l'écrit/lit.


def set_daily_start(
    guild_id: int, date_str: str, user_id: int, mode: str, difficulty: str = "normal"
) -> str:
    """Enregistre l'heure de départ serveur + la difficulté pour ce (joueur, mode, jour).

    INSERT OR IGNORE : le premier "Jouer" gagne, l'heure ET la difficulté sont
    immuables → le temps de réponse mesuré côté serveur ne peut pas être truqué,
    et on ne peut pas changer de difficulté après avoir vu/pas vu les options.
    Renvoie la difficulté effectivement verrouillée."""
    conn = get_conn()
    # Précision milliseconde (%f) : datetime('now') tronque à la seconde, ce qui
    # fausserait un temps de réponse affiché à 3 décimales / le tri par vitesse.
    conn.execute(
        "INSERT OR IGNORE INTO daily_start "
        "(guild_id, date, user_id, mode, started_at, difficulty) "
        "VALUES (?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f','now'), ?)",
        (guild_id, date_str, user_id, mode, difficulty),
    )
    conn.commit()
    return get_daily_difficulty(guild_id, date_str, user_id, mode) or difficulty


def get_daily_difficulty(
    guild_id: int, date_str: str, user_id: int, mode: str
) -> Optional[str]:
    """Difficulté verrouillée pour ce (joueur, mode, jour) : 'normal'|'hardcore',
    ou None si le joueur n'a pas encore cliqué "Jouer" dans ce mode."""
    row = get_conn().execute(
        "SELECT difficulty FROM daily_start "
        "WHERE guild_id = ? AND date = ? AND user_id = ? AND mode = ?",
        (guild_id, date_str, user_id, mode),
    ).fetchone()
    return row["difficulty"] if row else None


def get_start_elapsed_seconds(
    guild_id: int, date_str: str, user_id: int, mode: str
) -> Optional[float]:
    """Secondes écoulées depuis le "Jouer" (heure serveur) pour ce mode, ou None.

    Sert d'arbitre pour enregistrer un temps de réponse honnête (anti-triche sur
    la vitesse, qui départage le classement)."""
    row = get_conn().execute(
        "SELECT (julianday('now') - julianday(started_at)) * 86400.0 AS secs "
        "FROM daily_start WHERE guild_id = ? AND date = ? AND user_id = ? AND mode = ?",
        (guild_id, date_str, user_id, mode),
    ).fetchone()
    return row["secs"] if row and row["secs"] is not None else None


# --- Recherche d'auteurs (barre de recherche Hardcore) --------------------

def search_authors(guild_id: int, query: str, limit: int = 25) -> List[Dict]:
    """Recherche partielle d'auteurs connus (réponses possibles du mode auteur).

    Source : auteurs de messages (= les seules bonnes réponses possibles), enrichis
    de l'avatar via la table users. Exclut bots/supprimés/opt-out/blacklist.
    `query` vide → renvoie les premiers auteurs par ordre alpha.
    """
    like = f"%{(query or '').strip().lower()}%"
    blacklist = getattr(config, "BLACKLIST_USER_IDS", []) or []
    sql = [
        "SELECT m.author_id AS user_id, m.author_name AS name, u.avatar_url AS avatar_url",
        "FROM messages m",
        "LEFT JOIN users u ON u.guild_id = m.guild_id AND u.user_id = m.author_id",
        "WHERE m.guild_id = ?",
        "  AND LOWER(m.author_name) LIKE ?",
        "  AND m.author_id NOT IN (SELECT user_id FROM optout WHERE guild_id = ?)",
        "  AND LOWER(m.author_name) NOT LIKE 'deleted\\_user%' ESCAPE '\\'",
        "  AND LOWER(m.author_name) != 'deleted user'",
    ]
    params: List = [guild_id, like, guild_id]
    if blacklist:
        placeholders = ",".join("?" * len(blacklist))
        sql.append(f"  AND m.author_id NOT IN ({placeholders})")
        params.extend(blacklist)
    sql.append("GROUP BY m.author_id")
    sql.append("ORDER BY LOWER(m.author_name) ASC")
    sql.append("LIMIT ?")
    params.append(limit)
    rows = get_conn().execute("\n".join(sql), params).fetchall()
    return [
        {"user_id": r["user_id"], "name": r["name"], "avatar_url": r["avatar_url"]}
        for r in rows
    ]


def resolve_member_guess(guild_id: int, text: str, threshold: float = 0.74):
    """Relie une saisie libre (mode Hardcore) au membre visé, avec tolérance aux
    fautes de frappe et aux surnoms (aliases.py). Renvoie (user_id, name) du
    meilleur candidat au-dessus du seuil, ou None si trop incertain."""
    import aliases

    g = aliases.normalize(text)
    if not g:
        return None
    candidates = search_authors(guild_id, "", limit=2000)
    best = None
    best_score = 0.0
    for cand in candidates:
        for accepted in aliases.accepted_strings(cand["name"]):
            score = aliases.similarity(text, accepted)
            if score > best_score:
                best_score = score
                best = cand
    if best is not None and best_score >= threshold:
        return (best["user_id"], best["name"])
    return None


def search_members_ranked(guild_id: int, query: str, limit: int = 12) -> List[Dict]:
    """Suggestions Hardcore au fil de la frappe : membres dont le pseudo OU un
    alias matche la saisie.

    Saisie vide → TOUT le roster (ordre alpha), pour pouvoir choisir à la main sans
    taper. Sinon, ordonné : exact, puis COMMENCE par, puis CONTIENT, puis flou
    (typos), alphabétique en cas d'égalité. La recherche porte sur tout le roster
    (pas sur les 4 propositions du jour) → ne révèle pas la réponse.
    """
    import difflib

    import aliases

    q = aliases.normalize(query)
    if not q:
        # Saisie vide : liste complète des membres (alpha) à choisir à la main.
        return [
            {"user_id": c["user_id"], "name": c["name"],
             "avatar_url": c["avatar_url"], "alias": None}
            for c in search_authors(guild_id, "", limit=limit)
        ]

    # Seuil flou : en dessous, on n'affiche pas (évite le bruit type alias court qui
    # ressemble par hasard). Au-dessus, les vraies fautes de frappe remontent.
    FUZZY_MIN = 0.6

    scored = []
    for cand in search_authors(guild_id, "", limit=2000):
        name = cand["name"]
        norm_name = aliases.normalize(name)
        best = None  # (rank, -sim, matched_alias) ; rank: 0 exact,1 préfixe,2 contient,3 flou
        for s in aliases.accepted_strings(name):
            ns = aliases.normalize(s)
            if not ns:
                continue
            if ns == q:
                rank, sim = 0, 1.0
            elif ns.startswith(q):
                rank, sim = 1, 1.0
            elif q in ns:
                rank, sim = 2, 1.0
            else:
                # Ratio brut (sans bonus de contenance) : meilleur pour CLASSER les
                # typos (« kantarz » → kantraz avant un alias court contenu dedans).
                sim = difflib.SequenceMatcher(None, q, ns).ratio()
                if sim < FUZZY_MIN:
                    continue
                rank = 3
            key = (rank, -sim)
            if best is None or key < best[0]:
                # On n'affiche un surnom que si c'est lui (pas le pseudo) qui matche.
                alias = None if ns == norm_name else s
                best = (key, alias)
        if best is not None:
            (rank, neg_sim), alias = best
            scored.append((rank, neg_sim, norm_name, cand, alias))

    # Tri : tier (exact < préfixe < contient < flou), puis similarité décroissante,
    # puis ordre alpha. → la 1re ligne est le meilleur candidat (sélectionné par défaut).
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return [
        {
            "user_id": cand["user_id"],
            "name": cand["name"],
            "avatar_url": cand["avatar_url"],
            "alias": alias,
        }
        for _, _, _, cand, alias in scored[:limit]
    ]


# --- Scores ---------------------------------------------------------------

def record_answer(
    guild_id: int, user_id: int, name: str, correct: bool,
    mode: str = MODE_AUTHOR, points: int = 1,
) -> None:
    """Met à jour le score. `points` = points gagnés si `correct` (Normal 1,
    Hardcore 2). `correct`/`total` comptent toujours victoires/parties."""
    tbl = _tbl(mode, "scores")
    win = 1 if correct else 0
    pts = points if correct else 0
    conn = get_conn()
    conn.execute(
        f"INSERT INTO {tbl} (guild_id, user_id, name, correct, total, points) "
        "VALUES (?, ?, ?, ?, 1, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
        "  correct = correct + ?, total = total + 1, points = points + ?, "
        "  name = excluded.name",
        (guild_id, user_id, name, win, pts, win, pts),
    )
    conn.commit()


def get_leaderboard(
    guild_id: int, limit: Optional[int] = None, mode: str = MODE_AUTHOR
) -> List[Dict]:
    """Renvoie le classement des joueurs, trié par points (Hardcore = +2).

    Ordre : points décroissant, puis plus de victoires, puis meilleur ratio
    (moins de tentatives à victoires égales), puis temps moyen le plus rapide,
    puis nom. `limit=None` renvoie tout le monde.

    Chaque entrée est un dict : name, correct, total, points, current_streak,
    best_streak, avg_time_ms, last_broken_streak, last_broken_date,
    current_loss_streak.
    """
    sc_t = _tbl(mode, "scores")
    st_t = _tbl(mode, "streaks")
    da_t = _tbl(mode, "daily_attempts")
    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT sc.user_id,
               sc.name,
               sc.correct,
               sc.total,
               sc.points,
               COALESCE(st.current_streak, 0) AS current_streak,
               COALESCE(st.best_streak, 0)    AS best_streak,
               COALESCE(st.last_broken_streak, 0) AS last_broken_streak,
               st.last_broken_date,
               COALESCE(st.current_loss_streak, 0) AS current_loss_streak,
               (
                 SELECT AVG(da.time_taken_ms)
                 FROM {da_t} da
                 WHERE da.guild_id = sc.guild_id
                   AND da.user_id  = sc.user_id
                   AND da.time_taken_ms IS NOT NULL
               ) AS avg_time_ms,
               (
                 SELECT COUNT(*)
                 FROM {da_t} da
                 WHERE da.guild_id = sc.guild_id
                   AND da.user_id  = sc.user_id
                   AND da.difficulty = 'hardcore'
               ) AS hardcore_count
        FROM {sc_t} sc
        LEFT JOIN {st_t} st
          ON st.guild_id = sc.guild_id AND st.user_id = sc.user_id
        WHERE sc.guild_id = ?
        """,
        (guild_id,),
    ).fetchall()

    entries = [
        {
            "user_id": r["user_id"],
            "name": r["name"],
            "correct": r["correct"],
            "total": r["total"],
            "points": r["points"],
            "current_streak": r["current_streak"],
            "best_streak": r["best_streak"],
            "last_broken_streak": r["last_broken_streak"],
            "last_broken_date": r["last_broken_date"],
            "current_loss_streak": r["current_loss_streak"],
            "avg_time_ms": r["avg_time_ms"],
            "hardcore_count": r["hardcore_count"],
            "normal_count": max(0, r["total"] - r["hardcore_count"]),
        }
        for r in rows
    ]

    # Tri : points décroissant (le Hardcore vaut double), puis plus de victoires,
    # puis meilleur ratio, puis le plus rapide (None en dernier), puis le nom.
    entries.sort(
        key=lambda e: (
            -e["points"],
            -e["correct"],
            e["total"],
            e["avg_time_ms"] if e["avg_time_ms"] is not None else float("inf"),
            e["name"].lower(),
        )
    )
    return entries[:limit] if limit is not None else entries


def get_user_score(guild_id: int, user_id: int, mode: str = MODE_AUTHOR) -> Tuple[int, int]:
    """Renvoie (correct, total) pour un joueur. (0, 0) si jamais joué."""
    tbl = _tbl(mode, "scores")
    row = get_conn().execute(
        f"SELECT correct, total FROM {tbl} WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return (0, 0)
    return (row["correct"], row["total"])


# --- Streaks --------------------------------------------------------------

def update_streak(
    guild_id: int, user_id: int, today_str: str, is_correct: bool,
    mode: str = MODE_AUTHOR,
) -> Tuple[int, int]:
    """Met à jour la streak du joueur après une tentative. Renvoie (current, best).

    Règle : si correct et le dernier correct était hier → +1.
            si correct sans chaîne → 1.
            si raté → 0 (chaîne cassée).
    """
    tbl = _tbl(mode, "streaks")
    conn = get_conn()
    row = conn.execute(
        "SELECT current_streak, best_streak, last_correct_date, "
        "       last_broken_streak, last_broken_date, "
        "       current_loss_streak, last_loss_date "
        f"FROM {tbl} "
        "WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()

    today = date.fromisoformat(today_str)
    yesterday_iso = (today - timedelta(days=1)).isoformat()

    if row is None:
        current = 1 if is_correct else 0
        best = current
        last_correct = today_str if is_correct else None
        loss_streak = 0 if is_correct else 1
        last_loss = None if is_correct else today_str
        conn.execute(
            f"INSERT INTO {tbl} "
            "(guild_id, user_id, current_streak, best_streak, last_correct_date, "
            " last_broken_streak, last_broken_date, current_loss_streak, last_loss_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, current, best, last_correct, 0, None,
             loss_streak, last_loss),
        )
    else:
        if is_correct:
            current = row["current_streak"] + 1 if row["last_correct_date"] == yesterday_iso else 1
            last_correct = today_str
            last_broken_streak = row["last_broken_streak"]
            last_broken_date = row["last_broken_date"]
            # Une victoire remet à zéro la série de défaites.
            loss_streak = 0
            last_loss = row["last_loss_date"]
        else:
            streak_was_active = (
                row["current_streak"] > 0
                and row["last_correct_date"] == yesterday_iso
            )
            last_broken_streak = row["current_streak"] if streak_was_active else 0
            last_broken_date = today_str if last_broken_streak > 0 else row["last_broken_date"]
            current = 0
            last_correct = row["last_correct_date"]
            # Série de défaites : +1 si on avait déjà perdu hier, sinon repart à 1.
            loss_streak = (
                row["current_loss_streak"] + 1
                if row["last_loss_date"] == yesterday_iso
                else 1
            )
            last_loss = today_str
        best = max(row["best_streak"], current)
        conn.execute(
            f"UPDATE {tbl} "
            "SET current_streak = ?, best_streak = ?, last_correct_date = ?, "
            "    last_broken_streak = ?, last_broken_date = ?, "
            "    current_loss_streak = ?, last_loss_date = ? "
            "WHERE guild_id = ? AND user_id = ?",
            (
                current, best, last_correct,
                last_broken_streak, last_broken_date,
                loss_streak, last_loss,
                guild_id, user_id,
            ),
        )
    conn.commit()
    return (current, best)


def get_streak(
    guild_id: int, user_id: int, today_str: Optional[str] = None,
    mode: str = MODE_AUTHOR,
) -> Tuple[int, int]:
    """Renvoie (current, best). Si today_str est donné, retourne 0 si la chaîne
    est cassée (dernier correct strictement avant hier)."""
    tbl = _tbl(mode, "streaks")
    row = get_conn().execute(
        f"SELECT current_streak, best_streak, last_correct_date FROM {tbl} "
        "WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return (0, 0)

    current = row["current_streak"]
    best = row["best_streak"]
    if today_str is None or current == 0 or row["last_correct_date"] is None:
        return (current, best)

    today = date.fromisoformat(today_str)
    last = date.fromisoformat(row["last_correct_date"])
    if (today - last).days > 1:
        # Chaîne cassée : dernier correct ≥ 2 jours.
        return (0, best)
    return (current, best)


# --- Daily ----------------------------------------------------------------

def get_daily(guild_id: int, date_str: str, mode: str = MODE_AUTHOR) -> Optional[Dict]:
    """Renvoie le défi du jour (dict) ou None s'il n'existe pas encore.

    Couvre les modes 'author' (table daily) et 'media' (table media_daily) qui
    partagent la même structure. Le mode 'phrase' a get_phrase_daily.
    """
    tbl = _tbl(mode, "daily")
    row = get_conn().execute(
        f"SELECT message_id, channel_id, author_id, author_name, content, options "
        f"FROM {tbl} WHERE guild_id = ? AND date = ?",
        (guild_id, date_str),
    ).fetchone()
    if row is None:
        return None
    return {
        "message_id": row["message_id"],
        "channel_id": row["channel_id"],
        "author_id": row["author_id"],
        "author_name": row["author_name"],
        "content": row["content"],
        "options": [tuple(o) for o in json.loads(row["options"])],
    }


def create_daily_if_absent(
    guild_id: int,
    date_str: str,
    message_id: int,
    channel_id: int,
    author_id: int,
    author_name: str,
    content: str,
    options: List[Tuple[int, str]],
    mode: str = MODE_AUTHOR,
) -> bool:
    """Crée le défi du jour si aucun n'existe encore. Renvoie True si on l'a créé.

    Modes 'author' (daily) et 'media' (media_daily). Sécurisé contre les courses
    grâce à INSERT OR IGNORE.
    """
    tbl = _tbl(mode, "daily")
    conn = get_conn()
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {tbl} "
        "(guild_id, date, message_id, channel_id, author_id, author_name, content, options) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id, date_str, message_id, channel_id,
            author_id, author_name, content, json.dumps(options),
        ),
    )
    conn.commit()
    return cur.rowcount > 0


# --- Daily mode "phrase" --------------------------------------------------

def get_phrase_daily(guild_id: int, date_str: str) -> Optional[Dict]:
    """Renvoie le défi 'phrase' du jour, ou None.

    Structure de retour homogène avec get_daily pour réutiliser la webapp :
      - author_id / author_name = l'utilisateur cible (« Quelle phrase a écrit X ? »)
      - correct_id              = message_id de la bonne phrase
      - message_id / channel_id = la bonne phrase (pour le contexte au reveal)
      - content                 = texte de la bonne phrase
      - options                 = [(message_id, texte), ...] (4 propositions)
    """
    row = get_conn().execute(
        "SELECT target_author_id, target_author_name, correct_message_id, "
        "       channel_id, content, options "
        "FROM phrase_daily WHERE guild_id = ? AND date = ?",
        (guild_id, date_str),
    ).fetchone()
    if row is None:
        return None
    return {
        "author_id": row["target_author_id"],
        "author_name": row["target_author_name"],
        "correct_id": row["correct_message_id"],
        "message_id": row["correct_message_id"],
        "channel_id": row["channel_id"],
        "content": row["content"],
        "options": [tuple(o) for o in json.loads(row["options"])],
    }


def create_phrase_daily_if_absent(
    guild_id: int,
    date_str: str,
    target_author_id: int,
    target_author_name: str,
    correct_message_id: int,
    channel_id: int,
    content: str,
    options: List[Tuple[int, str]],
) -> bool:
    """Crée le défi 'phrase' du jour s'il n'existe pas. True si créé."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO phrase_daily "
        "(guild_id, date, target_author_id, target_author_name, correct_message_id, "
        " channel_id, content, options) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id, date_str, target_author_id, target_author_name,
            correct_message_id, channel_id, content, json.dumps(options),
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def is_daily_announced(guild_id: int, date_str: str, mode: str = MODE_AUTHOR) -> bool:
    """True si l'annonce du daily a déjà été envoyée pour ce jour."""
    tbl = _tbl(mode, "daily_announced")
    row = get_conn().execute(
        f"SELECT 1 FROM {tbl} WHERE guild_id = ? AND date = ?",
        (guild_id, date_str),
    ).fetchone()
    return row is not None


def mark_daily_announced(guild_id: int, date_str: str, mode: str = MODE_AUTHOR) -> bool:
    """Marque le daily comme annoncé. Renvoie False si déjà marqué (course)."""
    tbl = _tbl(mode, "daily_announced")
    conn = get_conn()
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {tbl} (guild_id, date, announced_at) "
        "VALUES (?, ?, datetime('now'))",
        (guild_id, date_str),
    )
    conn.commit()
    return cur.rowcount > 0


def get_daily_attempt(
    guild_id: int, date_str: str, user_id: int, mode: str = MODE_AUTHOR
) -> Optional[Dict]:
    tbl = _tbl(mode, "daily_attempts")
    row = get_conn().execute(
        f"SELECT user_name, guessed_id, correct, answered_at, time_taken_ms, difficulty "
        f"FROM {tbl} WHERE guild_id = ? AND date = ? AND user_id = ?",
        (guild_id, date_str, user_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "user_name": row["user_name"],
        "guessed_id": row["guessed_id"],
        "correct": bool(row["correct"]),
        "answered_at": row["answered_at"],
        "time_taken_ms": row["time_taken_ms"],
        "difficulty": row["difficulty"] if "difficulty" in row.keys() else "normal",
    }


def record_daily_attempt(
    guild_id: int,
    date_str: str,
    user_id: int,
    user_name: str,
    guessed_id: int,
    correct: bool,
    time_taken_ms: Optional[int] = None,
    mode: str = MODE_AUTHOR,
    difficulty: str = "normal",
) -> bool:
    """Enregistre la tentative. Renvoie False si déjà répondu (rejet).

    `time_taken_ms` est le temps que le joueur a mis à répondre (page web).
    None si non mesuré (ancienne client ou interface Discord).
    """
    tbl = _tbl(mode, "daily_attempts")
    if time_taken_ms is not None:
        # Bornes raisonnables : 0..24h.
        time_taken_ms = max(0, min(int(time_taken_ms), 24 * 60 * 60 * 1000))
    conn = get_conn()
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {tbl} "
        "(guild_id, date, user_id, user_name, guessed_id, correct, answered_at, "
        " time_taken_ms, difficulty) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)",
        (guild_id, date_str, user_id, user_name, guessed_id,
         1 if correct else 0, time_taken_ms, difficulty),
    )
    conn.commit()
    return cur.rowcount > 0


def get_daily_results(guild_id: int, date_str: str, mode: str = MODE_AUTHOR) -> List[Dict]:
    """Liste des tentatives du jour (avec avatar et temps de réponse quand connus)."""
    da_t = _tbl(mode, "daily_attempts")
    st_t = _tbl(mode, "streaks")
    rows = get_conn().execute(
        f"""
        SELECT da.date, da.user_id, da.user_name, da.correct, da.answered_at,
               da.guessed_id, da.time_taken_ms, da.difficulty, u.avatar_url,
               COALESCE(st.last_broken_streak, 0) AS last_broken_streak,
               st.last_broken_date
        FROM {da_t} da
        LEFT JOIN users u
          ON u.guild_id = da.guild_id AND u.user_id = da.user_id
        LEFT JOIN {st_t} st
          ON st.guild_id = da.guild_id AND st.user_id = da.user_id
        WHERE da.guild_id = ? AND da.date = ?
        ORDER BY da.answered_at ASC
        """,
        (guild_id, date_str),
    ).fetchall()
    return [
        {
            "date": r["date"],
            "user_id": r["user_id"],
            "user_name": r["user_name"],
            "correct": bool(r["correct"]),
            "answered_at": r["answered_at"],
            "guessed_id": r["guessed_id"],
            "time_taken_ms": r["time_taken_ms"],
            "difficulty": r["difficulty"] if "difficulty" in r.keys() else "normal",
            "avatar_url": r["avatar_url"],
            "last_broken_streak": r["last_broken_streak"],
            "last_broken_date": r["last_broken_date"],
        }
        for r in rows
    ]


def get_guess_distribution(
    guild_id: int, date_str: str, mode: str = MODE_AUTHOR
) -> Dict[int, int]:
    """Répartition des votes du jour : {id_deviné: nombre de joueurs}.

    Sert à afficher, au reveal, le % de joueurs ayant choisi chaque proposition
    (pour voir quelle réponse a le plus "baité"). Compte toutes les tentatives,
    y compris Hardcore (un guess Hardcore hors des 4 options n'est attribué à
    aucune proposition, ce qui est volontaire)."""
    da_t = _tbl(mode, "daily_attempts")
    rows = get_conn().execute(
        f"SELECT guessed_id, COUNT(*) AS c FROM {da_t} "
        "WHERE guild_id = ? AND date = ? GROUP BY guessed_id",
        (guild_id, date_str),
    ).fetchall()
    return {row["guessed_id"]: row["c"] for row in rows}


# --- Profils utilisateurs (nom + avatar) -----------------------------------

def upsert_user(guild_id: int, user_id: int, name: str, avatar_url: Optional[str]) -> None:
    """Insère ou met à jour le profil d'un joueur (utilisé par bot + webapp)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO users (guild_id, user_id, name, avatar_url) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
        "  name = excluded.name, avatar_url = excluded.avatar_url",
        (guild_id, user_id, name, avatar_url),
    )
    conn.commit()


def get_user(guild_id: int, user_id: int) -> Optional[Dict]:
    row = get_conn().execute(
        "SELECT name, avatar_url FROM users WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return {"name": row["name"], "avatar_url": row["avatar_url"]}
