"""Gestion des jours de grève : plan de transport par segment, grille officielle SNCF, alertes ciblées.

Principe : pour chaque segment suivi (UIC départ -> UIC arrivée) on compare l'horaire de BASE (théorique)
et l'horaire TEMPS RÉEL publié par la SNCF (plan de transport adapté). Un train théorique absent du
temps réel est « supprimé » ; un train dont les horaires diffèrent est « modifié ».
Une grille officielle de trains maintenus (fichier / liste) prime sur Navitia.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import bazo_alternatives as ALT
import bazo_geo
import bazo_notify
import bazo_sources_rt as S
import bazo_temps_reel as TR
from bazo_config import db_connect

logger = logging.getLogger("bazo.greve")
tz_paris = ZoneInfo("Europe/Paris")

SEUIL_MODIFIE_DEP_MIN = 3
SEUIL_MODIFIE_ARR_MIN = 5
SEUIL_AUTO_RATIO = 0.20
SEUIL_AUTO_MIN_SUPPR = 4
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

# Segments toujours suivis (axe Metz - Luxembourg dans les deux sens + Nancy).
SEGMENTS_COEUR = (
    ("Metz", "Luxembourg"), ("Luxembourg", "Metz"),
    ("Thionville", "Luxembourg"), ("Luxembourg", "Thionville"),
    ("Nancy", "Metz"), ("Metz", "Nancy"),
)

_SCHEMA_OK = False


def init_schema() -> None:
    global _SCHEMA_OK
    if _SCHEMA_OK:
        return
    TR.init_schema()
    conn = db_connect(timeout=10)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS greve_plan (
            date TEXT NOT NULL, uic_dep TEXT NOT NULL, uic_arr TEXT NOT NULL, numero_train TEXT NOT NULL,
            gare_depart TEXT, gare_arrivee TEXT,
            dep_base_ts INTEGER, arr_base_ts INTEGER, dep_rt_ts INTEGER, arr_rt_ts INTEGER,
            statut TEXT NOT NULL, statut_navitia TEXT, source TEXT, divergence INTEGER DEFAULT 0,
            note TEXT, updated_ts INTEGER,
            PRIMARY KEY (date, uic_dep, uic_arr, numero_train)
        );
        CREATE INDEX IF NOT EXISTS idx_greve_plan_date ON greve_plan(date, uic_dep, uic_arr);
        CREATE TABLE IF NOT EXISTS greve_meta (
            date TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'veille', source TEXT,
            nb_total INTEGER DEFAULT 0, nb_supprimes INTEGER DEFAULT 0, nb_modifies INTEGER DEFAULT 0,
            import_exhaustif INTEGER DEFAULT 0, import_ts INTEGER, note TEXT, updated_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS greve_import (
            date TEXT NOT NULL, numero_train TEXT NOT NULL, statut TEXT NOT NULL,
            heure_dep TEXT, note TEXT, ts INTEGER,
            PRIMARY KEY (date, numero_train)
        );
        """
    )
    conn.commit()
    conn.close()
    _SCHEMA_OK = True


# --------------------------------------------------------------------------
# Segments suivis
# --------------------------------------------------------------------------
def _uic(nom: str) -> str | None:
    return bazo_geo.uic_pour_gare(nom or "", "grand_est")


def segments_a_surveiller(conn, date_j: str | None = None) -> list[tuple[str, str]]:
    """Segments cœur + segments (aller/retour) des profils + trains habituels + check-in récents."""
    segs: list[tuple[str, str]] = []

    def ajouter(a, b):
        ua, ub = _uic(a), _uic(b)
        if ua and ub and ua != ub and (ua, ub) not in segs:
            segs.append((ua, ub))

    for a, b in SEGMENTS_COEUR:
        ajouter(a, b)
    for r in conn.execute("SELECT gare_depart, gare_retour FROM inscriptions").fetchall():
        ajouter(r[0], r[1])
        ajouter(r[1], r[0])
    for r in conn.execute("SELECT DISTINCT gare_depart, gare_arrivee FROM trains_habituels").fetchall():
        ajouter(r[0], r[1])
    return segs


# --------------------------------------------------------------------------
# Plan de transport
# --------------------------------------------------------------------------
def _date_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz_paris).strftime("%Y-%m-%d")


def lister_trains_segment(date_j: str, uic_dep: str, uic_arr: str, fraicheur: str, pages_max: int = 4) -> dict:
    """{numero: candidat} de tous les trains directs du jour sur le segment (pagination Navitia)."""
    out: dict[str, dict] = {}
    curseur = datetime.strptime(date_j, "%Y-%m-%d").replace(hour=0, minute=0, tzinfo=tz_paris)
    for _ in range(pages_max):
        lot = ALT.journeys_directs(uic_dep, uic_arr, curseur, count=30, fraicheur=fraicheur)
        if not lot:
            break
        nouveaux = 0
        for c in lot:
            if _date_ts(c["dep_plan"]) != date_j:
                continue
            if c["numero"] and c["numero"] not in out:
                out[c["numero"]] = c
                nouveaux += 1
        dernier = max(c["dep_plan"] for c in lot)
        if _date_ts(dernier) != date_j or nouveaux == 0:
            break
        curseur = datetime.fromtimestamp(dernier + 60, tz_paris)
    return out


def comparer_base_temps_reel(base: dict, rt: dict) -> list[dict]:
    """Statuts maintenu / modifie / supprime / ajoute. Pure, testable."""
    out = []
    for numero, b in base.items():
        r = rt.get(numero)
        if r is None:
            out.append({"numero": numero, "statut": "supprime", "base": b, "rt": None})
            continue
        d_dep = abs(r["dep_rt"] - b["dep_plan"]) / 60
        d_arr = abs(r["arr_rt"] - b["arr_plan"]) / 60
        modifie = d_dep >= SEUIL_MODIFIE_DEP_MIN or d_arr >= SEUIL_MODIFIE_ARR_MIN
        out.append({"numero": numero, "statut": "modifie" if modifie else "maintenu", "base": b, "rt": r})
    for numero, r in rt.items():
        if numero not in base:
            out.append({"numero": numero, "statut": "ajoute", "base": None, "rt": r})
    return out


def _base_en_cache(conn, date_j, uic_dep, uic_arr) -> dict:
    rows = conn.execute(
        "SELECT numero_train, dep_base_ts, arr_base_ts FROM greve_plan WHERE date=? AND uic_dep=? AND uic_arr=? "
        "AND dep_base_ts IS NOT NULL", (date_j, uic_dep, uic_arr),
    ).fetchall()
    return {
        r[0]: {"numero": r[0], "dep_plan": r[1], "dep_rt": r[1], "arr_plan": r[2], "arr_rt": r[2],
               "ret_dep": 0, "ret_arr": 0, "annule": False}
        for r in rows
    }


def charger_import(conn, date_j: str) -> tuple[dict, bool]:
    rows = conn.execute("SELECT numero_train, statut, heure_dep FROM greve_import WHERE date=?", (date_j,)).fetchall()
    meta = conn.execute("SELECT import_exhaustif FROM greve_meta WHERE date=?", (date_j,)).fetchone()
    return {r[0]: {"statut": r[1], "heure_dep": r[2]} for r in rows}, bool(meta and meta[0])


def appliquer_import(statut_nav: str, numero: str, imports: dict, exhaustif: bool, rt_existe: bool) -> tuple[str, int]:
    """Statut final après grille officielle. Retourne (statut, divergence)."""
    if not imports:
        return statut_nav, 0
    imp = imports.get(numero)
    if imp:
        s = imp["statut"]
        if s == "maintenu":
            final = "maintenu" if statut_nav in ("maintenu", "supprime") else statut_nav
            return final, 1 if statut_nav == "supprime" else 0
        return s, 1 if (s == "supprime" and rt_existe) else 0
    if exhaustif:
        return "supprime", 1 if rt_existe and statut_nav in ("maintenu", "modifie") else 0
    return statut_nav, 0


def rafraichir_plan(conn, date_j: str, segments: list[tuple[str, str]] | None = None,
                    finals: dict | None = None) -> dict:
    """Recalcule le plan de grève de la journée. Les horaires de base sont lus une seule fois par segment."""
    init_schema()
    segments = segments or segments_a_surveiller(conn, date_j)
    imports, exhaustif = charger_import(conn, date_j)
    now_ts = int(time.time())
    stats = {"segments": 0, "total": 0, "supprimes": 0, "modifies": 0, "erreurs": 0}
    for uic_dep, uic_arr in segments:
        base = _base_en_cache(conn, date_j, uic_dep, uic_arr)
        if not base:
            base = lister_trains_segment(date_j, uic_dep, uic_arr, "base_schedule")
        rt = lister_trains_segment(date_j, uic_dep, uic_arr, "realtime")
        if not base and not rt:
            stats["erreurs"] += 1
            continue
        stats["segments"] += 1
        for item in comparer_base_temps_reel(base, rt):
            numero, statut_nav, b, r = item["numero"], item["statut"], item["base"], item["rt"]
            source = "navitia"
            statut = statut_nav
            # Vérité terrain : annulation vue dans GTFS-RT / CFL
            if finals:
                e = finals.get((date_j, numero))
                if e and e.get("annule") and statut != "supprime":
                    statut, source = "supprime", "temps_reel"
            statut, divergence = appliquer_import(statut, numero, imports, exhaustif, r is not None)
            if imports and (numero in imports or exhaustif):
                source = "import_sncf"
            conn.execute(
                """INSERT INTO greve_plan (date, uic_dep, uic_arr, numero_train, gare_depart, gare_arrivee,
                       dep_base_ts, arr_base_ts, dep_rt_ts, arr_rt_ts, statut, statut_navitia, source,
                       divergence, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(date, uic_dep, uic_arr, numero_train) DO UPDATE SET
                       dep_base_ts = COALESCE(greve_plan.dep_base_ts, excluded.dep_base_ts),
                       arr_base_ts = COALESCE(greve_plan.arr_base_ts, excluded.arr_base_ts),
                       dep_rt_ts = excluded.dep_rt_ts, arr_rt_ts = excluded.arr_rt_ts,
                       statut = excluded.statut, statut_navitia = excluded.statut_navitia,
                       source = excluded.source, divergence = excluded.divergence, updated_ts = excluded.updated_ts""",
                (date_j, uic_dep, uic_arr, numero, S.nom_gare(uic_dep), S.nom_gare(uic_arr),
                 b["dep_plan"] if b else None, b["arr_plan"] if b else None,
                 r["dep_rt"] if r else None, r["arr_rt"] if r else None,
                 statut, statut_nav, source, divergence, now_ts),
            )
            stats["total"] += 1
            stats["supprimes"] += statut == "supprime"
            stats["modifies"] += statut == "modifie"
        conn.commit()          # jamais d'écriture en attente pendant l'appel HTTP du segment suivant
    mode = detecter_mode(conn, date_j, stats)
    conn.execute(
        """INSERT INTO greve_meta (date, mode, source, nb_total, nb_supprimes, nb_modifies, updated_ts)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET mode = excluded.mode, nb_total = excluded.nb_total,
               nb_supprimes = excluded.nb_supprimes, nb_modifies = excluded.nb_modifies,
               updated_ts = excluded.updated_ts,
               source = COALESCE(greve_meta.source, excluded.source)""",
        (date_j, mode, "navitia", stats["total"], stats["supprimes"], stats["modifies"], now_ts),
    )
    conn.commit()
    stats["mode"] = mode
    return stats


def detecter_mode(conn, date_j: str, stats: dict) -> str:
    """veille | auto (grève probable détectée) | actif (import officiel) | force (admin)."""
    row = conn.execute("SELECT mode, import_ts FROM greve_meta WHERE date=?", (date_j,)).fetchone()
    if row and row[0] in ("force", "actif"):
        return row[0]
    if row and row[1]:
        return "actif"
    total = stats.get("total", 0)
    if total and stats["supprimes"] >= SEUIL_AUTO_MIN_SUPPR and stats["supprimes"] / total >= SEUIL_AUTO_RATIO:
        return "auto"
    return "veille"


def greve_active(conn, date_j: str) -> bool:
    row = conn.execute("SELECT mode FROM greve_meta WHERE date=?", (date_j,)).fetchone()
    return bool(row and row[0] in ("auto", "actif", "force"))


def forcer_mode(date_j: str, mode: str = "force", note: str = "") -> None:
    init_schema()
    conn = db_connect(timeout=10)
    conn.execute(
        "INSERT INTO greve_meta (date, mode, source, note, updated_ts) VALUES (?,?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET mode=excluded.mode, note=excluded.note, updated_ts=excluded.updated_ts",
        (date_j, mode, "admin", note, int(time.time())),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Import de la grille officielle SNCF
# --------------------------------------------------------------------------
def parser_liste_trains(texte: str) -> list[dict]:
    """Accepte : liste de numéros (séparateurs libres) ou CSV `numero[,statut[,heure_dep]]`.
    Retourne [{"numero","statut","heure_dep"}]. Statut par défaut : maintenu."""
    texte = str(texte or "").strip()
    if not texte:
        return []
    lignes = [l for l in texte.splitlines() if l.strip()]
    entetes_csv = "," in lignes[0] or ";" in lignes[0]
    if entetes_csv and re.search(r"[a-zA-Z]", lignes[0]):
        delim = ";" if lignes[0].count(";") > lignes[0].count(",") else ","
        rdr = csv.DictReader(io.StringIO(texte), delimiter=delim)
        champs = {c.lower().strip(): c for c in (rdr.fieldnames or [])}
        cle_num = next((champs[k] for k in champs if k in ("numero", "numéro", "train", "numero_train", "n°")), None)
        cle_st = next((champs[k] for k in champs if k in ("statut", "etat", "état", "status")), None)
        cle_h = next((champs[k] for k in champs if k in ("heure", "heure_dep", "depart", "départ")), None)
        out = []
        for row in rdr:
            m = re.search(r"\d{3,6}", str(row.get(cle_num, "")) if cle_num else "")
            if not m:
                continue
            st = str(row.get(cle_st, "maintenu")).strip().lower() if cle_st else "maintenu"
            st = "supprime" if st.startswith(("supp", "annul")) else ("modifie" if st.startswith("modif") else "maintenu")
            out.append({"numero": m.group(0), "statut": st, "heure_dep": (row.get(cle_h) or "").strip() if cle_h else ""})
        return out
    vus, out = set(), []
    for m in re.finditer(r"\b\d{4,6}\b", texte):
        if m.group(0) not in vus:
            vus.add(m.group(0))
            out.append({"numero": m.group(0), "statut": "maintenu", "heure_dep": ""})
    return out


def importer_grille_officielle(date_j: str, trains: list[dict], exhaustif: bool = True,
                               note: str = "grille SNCF") -> dict:
    """Enregistre la grille officielle et recalcule le plan. `exhaustif=True` : tout train du plan absent
    de la liste est considéré supprimé."""
    init_schema()
    conn = db_connect(timeout=10)
    now_ts = int(time.time())
    conn.execute("DELETE FROM greve_import WHERE date=?", (date_j,))
    for t in trains:
        conn.execute(
            "INSERT OR REPLACE INTO greve_import (date, numero_train, statut, heure_dep, note, ts) VALUES (?,?,?,?,?,?)",
            (date_j, t["numero"], t["statut"], t.get("heure_dep") or "", note, now_ts),
        )
    conn.execute(
        "INSERT INTO greve_meta (date, mode, source, import_exhaustif, import_ts, note, updated_ts) "
        "VALUES (?, 'actif', 'import_sncf', ?, ?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET mode='actif', source='import_sncf', import_exhaustif=excluded.import_exhaustif, "
        "import_ts=excluded.import_ts, note=excluded.note, updated_ts=excluded.updated_ts",
        (date_j, 1 if exhaustif else 0, now_ts, note, now_ts),
    )
    conn.commit()
    stats = rafraichir_plan(conn, date_j)
    conn.close()
    return {"nb_importes": len(trains), **stats}


# --------------------------------------------------------------------------
# Trains habituels
# --------------------------------------------------------------------------
def definir_train_habituel(id_telegram: str, gare_depart: str, gare_arrivee: str, heure_theo: str,
                           numero_train: str | None = None, jours: str = "") -> None:
    init_schema()
    conn = db_connect(timeout=10)
    conn.execute(
        "INSERT INTO trains_habituels (id_telegram, gare_depart, gare_arrivee, heure_theo, numero_train, jours, manuel, updated_ts) "
        "VALUES (?,?,?,?,?,?,1,?) ON CONFLICT(id_telegram, gare_depart, gare_arrivee, heure_theo) DO UPDATE SET "
        "numero_train=excluded.numero_train, jours=excluded.jours, manuel=1, updated_ts=excluded.updated_ts",
        (str(id_telegram), bazo_geo.nom_canonique_gare(gare_depart), bazo_geo.nom_canonique_gare(gare_arrivee),
         heure_theo, numero_train, jours, int(time.time())),
    )
    conn.commit()
    conn.close()


def traiter_commande_montrain(id_telegram: str, texte: str) -> str:
    """Réponse à `/montrain 08h33` (ou `/montrain effacer`). Le train est rattaché au trajet du profil
    (aller si l'heure tombe dans le créneau aller, sinon retour)."""
    init_schema()
    arg = re.sub(r"^/montrain(@\w+)?", "", str(texte or ""), flags=re.I).strip()
    conn = db_connect(timeout=10)
    try:
        user = TR.charger_inscriptions(conn).get(str(id_telegram))
        if not user or not user.get("gare_depart") or not user.get("gare_retour"):
            return "Je n'ai pas encore ton trajet. Termine d'abord ton inscription avec /start."
        if arg.lower() in ("effacer", "supprimer", "reset"):
            conn.execute("DELETE FROM trains_habituels WHERE id_telegram=? AND manuel=1", (str(id_telegram),))
            conn.commit()
            return "C'est fait : tes trains habituels déclarés sont effacés."
        if not arg:
            lignes = [f"• {r[0]} → {r[1]} : {r[2]}" for r in conn.execute(
                "SELECT gare_depart, gare_arrivee, heure_theo FROM trains_habituels WHERE id_telegram=? ORDER BY heure_theo",
                (str(id_telegram),)).fetchall()]
            base = "\n".join(lignes) if lignes else "Aucun train habituel enregistré."
            return f"*Tes trains habituels*\n{base}\n\nPour en ajouter : /montrain 08h33"
        m = re.search(r"\b(\d{1,2})\s*[hH:]\s*(\d{2})\b", arg)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return "Format attendu : /montrain 08h33 (heure de départ de ton train à ta gare)."
        h, mn = int(m.group(1)), int(m.group(2))
        heure = f"{h:02d}h{mn:02d}"
        fen_a, fen_r = TR._fenetre(user.get("heures")), TR._fenetre(user.get("heures_retour"))
        if fen_a and fen_a[0] <= h < fen_a[1] + 1:
            g_d, g_a = user["gare_depart"], user["gare_retour"]
        elif fen_r and fen_r[0] <= h < fen_r[1] + 1:
            g_d, g_a = user["gare_retour"], user["gare_depart"]
        else:
            g_d, g_a = (user["gare_depart"], user["gare_retour"]) if h < 13 else (user["gare_retour"], user["gare_depart"])
    finally:
        conn.close()
    nums = re.findall(r"\b\d{4,6}\b", arg)
    definir_train_habituel(str(id_telegram), g_d, g_a, heure, numero_train=nums[0] if nums else None)
    return (f"✅ Noté : ton train habituel est le *{heure}* ({bazo_geo.nom_canonique_gare(g_d)} → "
            f"{bazo_geo.nom_canonique_gare(g_a)}).\nJe te préviens s'il est supprimé ou très en retard, "
            "et je te proposerai le train précédent / suivant qui circule vraiment.")


def deduire_trains_habituels(conn, min_obs: int = 2, depuis_jours: int = 90) -> int:
    """Déduit les trains habituels des check-in déclarés (même gare, heure et jour de semaine, >= min_obs)."""
    init_schema()
    limite = (datetime.now(tz_paris) - timedelta(days=depuis_jours)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """SELECT id_telegram, gare_depart, gare_arrivee, heure_theo, numero_train, date FROM trajets_valides
           WHERE origine IN ('checkin','confirme_usager') AND statut_validation != 'annule'
             AND date >= ? AND gare_depart IS NOT NULL AND gare_arrivee IS NOT NULL""", (limite,),
    ).fetchall()
    groupes: dict[tuple, dict] = {}
    for idt, g_d, g_a, h, num, d in rows:
        cle = (str(idt), bazo_geo.nom_canonique_gare(g_d), bazo_geo.nom_canonique_gare(g_a), h)
        g = groupes.setdefault(cle, {"n": 0, "jours": set(), "num": {}})
        g["n"] += 1
        try:
            g["jours"].add(datetime.strptime(d, "%Y-%m-%d").weekday())
        except ValueError:
            pass
        g["num"][num] = g["num"].get(num, 0) + 1
    nb = 0
    for (idt, g_d, g_a, h), g in groupes.items():
        if g["n"] < min_obs:
            continue
        num = max(g["num"], key=g["num"].get)
        conn.execute(
            "INSERT INTO trains_habituels (id_telegram, gare_depart, gare_arrivee, heure_theo, numero_train, jours, nb_obs, manuel, updated_ts) "
            "VALUES (?,?,?,?,?,?,?,0,?) ON CONFLICT(id_telegram, gare_depart, gare_arrivee, heure_theo) DO UPDATE SET "
            "nb_obs=excluded.nb_obs, numero_train=CASE WHEN trains_habituels.manuel=1 THEN trains_habituels.numero_train ELSE excluded.numero_train END, "
            "jours=CASE WHEN trains_habituels.manuel=1 THEN trains_habituels.jours ELSE excluded.jours END, "
            "updated_ts=excluded.updated_ts",
            (idt, g_d, g_a, h, num, ",".join(str(x) for x in sorted(g["jours"])), g["n"], int(time.time())),
        )
        nb += 1
    conn.commit()
    return nb


# --------------------------------------------------------------------------
# Évaluation par utilisateur + message
# --------------------------------------------------------------------------
def _plan_segment(conn, date_j: str, uic_dep: str, uic_arr: str) -> list[dict]:
    rows = conn.execute(
        "SELECT numero_train, dep_base_ts, arr_base_ts, dep_rt_ts, arr_rt_ts, statut FROM greve_plan "
        "WHERE date=? AND uic_dep=? AND uic_arr=? ORDER BY COALESCE(dep_base_ts, dep_rt_ts)", (date_j, uic_dep, uic_arr),
    ).fetchall()
    return [{"numero": r[0], "dep_base": r[1], "arr_base": r[2], "dep_rt": r[3], "arr_rt": r[4], "statut": r[5]}
            for r in rows]


def _icone(statut: str) -> str:
    return {"maintenu": "✅", "modifie": "⚠️", "supprime": "❌", "ajoute": "➕"}.get(statut, "❔")


def _libelle_train(t: dict) -> str:
    h = S.epoch_vers_hhmm(t["dep_base"] or t["dep_rt"])
    return f"{h} {_icone(t['statut'])}"


def segments_utilisateur(conn, user: dict, date_j: str) -> list[dict]:
    """Segments aller / retour actifs ce jour-là pour un profil."""
    jour = datetime.strptime(date_j, "%Y-%m-%d").weekday()
    if not TR._jours_ok(user.get("jours_actifs"), datetime.strptime(date_j, "%Y-%m-%d")):
        return []
    out = []
    for g_d, g_a, h in ((user["gare_depart"], user["gare_retour"], user["heures"]),
                        (user["gare_retour"], user["gare_depart"], user["heures_retour"])):
        fen = TR._fenetre(h)
        u_d, u_a = _uic(g_d), _uic(g_a)
        if fen and u_d and u_a and u_d != u_a:
            out.append({"gare_depart": g_d, "gare_arrivee": g_a, "uic_dep": u_d, "uic_arr": u_a, "fenetre": fen})
    return out


def habituels_pour(conn, idt: str, uic_dep: str, uic_arr: str, jour: int) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT gare_depart, gare_arrivee, heure_theo, numero_train, jours FROM trains_habituels WHERE id_telegram=?",
        (str(idt),),
    ).fetchall():
        if _uic(r[0]) == uic_dep and _uic(r[1]) == uic_arr:
            lst = [int(x) for x in str(r[4] or "").split(",") if x.strip().isdigit()]
            if not lst or jour in lst:
                out.append({"heure_theo": r[2], "numero": r[3]})
    return out


_cache_alt: dict = {}


def _alternatives_cache(date_j, seg, ref, now, finals, statuts, numero) -> dict:
    """Évite de rappeler Navitia pour chaque usager d'un même segment (TTL 10 min)."""
    cle = (date_j, seg["uic_dep"], seg["uic_arr"], ref, numero, int(now.timestamp()) // 600)
    if cle not in _cache_alt:
        if len(_cache_alt) > 200:
            _cache_alt.clear()
        _cache_alt[cle] = ALT.chercher_alternatives(
            date_j, seg["uic_dep"], seg["uic_arr"], ref, now, finals=finals, statuts_plan=statuts, exclure={numero},
        )
    return _cache_alt[cle]


def construire_message_utilisateur(conn, user: dict, date_j: str, now: datetime, finals: dict | None = None,
                                   maj: bool = False, jour_j: bool = False) -> tuple[str | None, str]:
    """Retourne (message, signature). message=None si rien de pertinent à dire."""
    d = datetime.strptime(date_j, "%Y-%m-%d")
    jour = d.weekday()
    blocs, sig_parts = [], []
    for seg in segments_utilisateur(conn, user, date_j):
        plan = _plan_segment(conn, date_j, seg["uic_dep"], seg["uic_arr"])
        if not plan:
            continue
        statuts = {t["numero"]: t["statut"] for t in plan}
        h1, h2 = seg["fenetre"]
        dans_fenetre = [t for t in plan if t["statut"] != "ajoute"
                        and h1 <= datetime.fromtimestamp(t["dep_base"] or t["dep_rt"], tz_paris).hour < h2]
        titre = f"*{TR._nom(seg['gare_depart'])} → {TR._nom(seg['gare_arrivee'])}*"
        lignes = [titre]
        habs = habituels_pour(conn, user["id_telegram"], seg["uic_dep"], seg["uic_arr"], jour)
        for hab in habs:
            t = next((x for x in plan if (hab["numero"] and x["numero"] == hab["numero"])
                      or S.epoch_vers_hhmm(x["dep_base"] or x["dep_rt"]) == hab["heure_theo"]), None)
            if t is None:
                lignes.append(f"🚆 Ton train de *{hab['heure_theo']}* : non trouvé dans les horaires du jour.")
                sig_parts.append(f"{hab['heure_theo']}:?")
                continue
            h = S.epoch_vers_hhmm(t["dep_base"] or t["dep_rt"])
            sig_parts.append(f"{t['numero']}:{t['statut']}:{S.epoch_vers_hhmm(t['dep_rt'])}:{S.epoch_vers_hhmm(t['arr_rt'])}")
            if t["statut"] == "maintenu":
                lignes.append(f"🚆 Ton train de *{h}* ({t['numero']}) : ✅ *maintenu*, horaires inchangés "
                              f"(arrivée {S.epoch_vers_hhmm(t['arr_rt'])}).")
                continue
            if t["statut"] == "modifie":
                lignes.append(f"🚆 Ton train de *{h}* ({t['numero']}) : ⚠️ *maintenu avec horaires modifiés* — "
                              f"départ *{S.epoch_vers_hhmm(t['dep_rt'])}*, arrivée *{S.epoch_vers_hhmm(t['arr_rt'])}*.")
                if t["arr_rt"] and t["arr_base"] and (t["arr_rt"] - t["arr_base"]) / 60 < 20:
                    continue
            else:
                lignes.append(f"🚆 Ton train de *{h}* ({t['numero']}) : ❌ *SUPPRIMÉ*.")
            ref = t["dep_base"] or t["dep_rt"]
            alts = _alternatives_cache(date_j, seg, ref, now, finals, statuts, t["numero"])
            lignes.append(alts["texte"])
            sig_parts.append(f"alt:{(alts['precedent'] or {}).get('numero')}:{(alts['suivant'] or {}).get('numero')}")
        if dans_fenetre:
            resume = " · ".join(_libelle_train(t) for t in dans_fenetre)
            lignes.append(f"🕗 Créneau {h1}h-{h2}h : {resume}")
            sig_parts.append("fen:" + ",".join(f"{t['numero']}{t['statut'][0]}" for t in dans_fenetre))
            if not habs:
                lignes.append("_Dis-moi ton train habituel avec /montrain 08h33 pour une alerte personnalisée._")
        if len(lignes) > 1:
            blocs.append("\n".join(lignes))
    if not blocs:
        return None, ""
    nb_supp = conn.execute(
        "SELECT nb_supprimes, nb_total FROM greve_meta WHERE date=?", (date_j,)).fetchone() or (0, 0)
    quand = "AUJOURD'HUI" if jour_j else f"demain ({JOURS_FR[jour]} {d.strftime('%d/%m')})"
    entete = f"{'🔄 *Mise à jour* — ' if maj else ''}🚨 *Grève {quand}*"
    pied = "_Source : horaires temps réel SNCF (plan de transport adapté). Mise à jour automatique si le plan change._"
    msg = entete + "\n\n" + "\n\n".join(blocs) + "\n\n" + pied
    sig = hashlib.sha1("|".join(sig_parts).encode()).hexdigest()[:12]
    return msg, sig


def notifier_greve(conn, date_j: str, now: datetime, finals: dict | None = None, envoyer=None,
                   jour_j: bool = False) -> int:
    """Envoie à chaque usager concerné le statut de son train habituel / créneau. Une seule fois par
    version du plan (signature)."""
    envoyer = envoyer or bazo_notify.envoyer
    if not greve_active(conn, date_j):
        return 0
    nb = 0
    inscriptions = TR.charger_inscriptions(conn)
    for idt, user in inscriptions.items():
        if not TR.peut_recevoir(user, datetime.strptime(date_j, "%Y-%m-%d").replace(tzinfo=tz_paris)):
            continue
        msg, sig = construire_message_utilisateur(conn, user, date_j, now, finals, jour_j=jour_j)
        if not msg:
            continue
        precedent = conn.execute(
            "SELECT cle FROM alertes_temps_reel WHERE id_telegram=? AND date=? AND cle LIKE 'greve:%' ORDER BY ts DESC LIMIT 1",
            (idt, date_j),
        ).fetchone()
        cle = f"greve:{sig}"
        if TR.deja_alerte(conn, idt, date_j, cle):
            continue
        if precedent:
            msg = msg.replace("🚨 *Grève", "🔄 *Mise à jour* — 🚨 *Grève", 1)
        if envoyer(idt, msg):
            nb += 1
            TR.marquer_alerte(conn, idt, date_j, cle)
        conn.commit()
    return nb


# --------------------------------------------------------------------------
# Veille planifiée (appelée à chaque cycle du service temps réel)
# --------------------------------------------------------------------------
def _derniere_execution(conn, cle: str) -> int:
    row = conn.execute("SELECT valeur FROM memoire_radar WHERE cle=?", (cle,)).fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _noter_execution(conn, cle: str, ts: int) -> None:
    conn.execute(
        "INSERT INTO memoire_radar (cle, valeur, updated_at) VALUES (?,?,datetime('now')) "
        "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur, updated_at=excluded.updated_at", (cle, str(ts)),
    )


def intervalle_veille_s(now: datetime, jour_j: bool, actif: bool) -> int | None:
    """None = pas de rafraîchissement à cette heure."""
    h = now.hour + now.minute / 60
    if jour_j:
        if 4.5 <= h < 10:
            return 15 * 60
        if 10 <= h < 21:
            return 30 * 60 if actif else 60 * 60
        return None
    if h >= 17:
        return 30 * 60 if h < 23 else None
    if 12 <= h < 17:
        return 90 * 60       # détection précoce (la grille SNCF paraît souvent à 17h)
    return None


def veille_greve(now: datetime | None = None, finals: dict | None = None, envoyer=None, simuler: bool = False) -> dict:
    """J-1 (dès 17h00) et jour J : rafraîchit le plan, détecte la grève, alerte les usagers concernés."""
    init_schema()
    now = now or datetime.now(tz_paris)
    conn = db_connect(timeout=10)
    resume = {}
    try:
        demain = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        aujourdhui = now.strftime("%Y-%m-%d")
        for date_j, jour_j in ((demain, False), (aujourdhui, True)):
            actif = greve_active(conn, date_j)
            interv = intervalle_veille_s(now, jour_j, actif)
            if interv is None:
                continue
            cle = f"greve_refresh:{date_j}"
            cle_17h = f"greve_17h:{date_j}"
            premiere_17h = (not jour_j) and now.hour >= 17 and not _derniere_execution(conn, cle_17h)
            if not premiere_17h and int(now.timestamp()) - _derniere_execution(conn, cle) < interv:
                continue
            if premiere_17h:
                _noter_execution(conn, cle_17h, int(now.timestamp()))
            _noter_execution(conn, cle, int(now.timestamp()))
            conn.commit()
            stats = rafraichir_plan(conn, date_j, finals=finals)
            resume[date_j] = stats
            if not actif and stats.get("mode") in ("auto", "actif", "force"):
                logger.warning("[GREVE] Mode %s détecté pour %s : %s trains supprimés / %s",
                               stats["mode"], date_j, stats["supprimes"], stats["total"])
            # Envoi seulement à partir de 17h00 la veille ; toute la journée le jour J.
            if jour_j or now.hour >= 17:
                resume[date_j]["messages"] = notifier_greve(conn, date_j, now, finals, envoyer, jour_j=jour_j)
    finally:
        conn.close()
    return resume
