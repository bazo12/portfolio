#!/usr/bin/env python3
"""Outils opérateur : grève, grille SNCF, trains habituels, retard cumulé, répétition à blanc.

Aucune commande n'envoie de message aux usagers (sauf `repetition --reel`, jamais utilisé par défaut).
Pour travailler sur une copie : BAZO_DB_PATH=/tmp/copie.db python3 bazo_greve_admin.py ...

  statut [--date AAAA-MM-JJ]
  forcer --date D [--mode force|actif|veille|auto] [--note TXT]
  importer --date D --fichier F.csv|F.txt [--non-exhaustif]
  habituel --id TG --depart GARE --arrivee GARE --heure 08h33 [--numero N]
  report --id TG --date D --train1 N1 --train2 N2 --depart GARE --arrivee GARE --rupture GARE [--arrivee-reelle 10h54]
  repetition [--date D]        (cycle complet en simulation : aucun envoi)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

tz = ZoneInfo("Europe/Paris")


def _g():
    import bazo_greve as G
    G.init_schema()
    return G


def cmd_statut(a):
    from bazo_config import db_connect
    _g()
    conn = db_connect(timeout=10)
    dates = [a.date] if a.date else [(datetime.now(tz) + timedelta(days=i)).strftime("%Y-%m-%d") for i in (0, 1)]
    for d in dates:
        meta = conn.execute("SELECT mode, source, nb_total, nb_supprimes, nb_modifies, import_exhaustif, updated_ts FROM greve_meta WHERE date=?", (d,)).fetchone()
        rep = conn.execute("SELECT statut, COUNT(*) FROM greve_plan WHERE date=? GROUP BY 1", (d,)).fetchall()
        print(d, "meta:", meta, "plan:", rep)
    sante = conn.execute("SELECT valeur FROM memoire_radar WHERE cle='temps_reel_sante'").fetchone()
    print("santé service:", sante[0] if sante else "(jamais exécuté)")
    ev = conn.execute("SELECT date, numero_train, type, gare, retard_min FROM evenements_trains ORDER BY id DESC LIMIT 10").fetchall() \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='evenements_trains'").fetchone() else []
    for e in ev:
        print("  événement:", e)
    conn.close()


def cmd_forcer(a):
    G = _g()
    G.forcer_mode(a.date, a.mode, a.note or "")
    print(f"Mode {a.mode} enregistré pour {a.date}. Le plan est recalculé au prochain cycle du service.")


def cmd_importer(a):
    G = _g()
    with open(a.fichier, encoding="utf-8") as f:
        trains = G.parser_liste_trains(f.read())
    if not trains:
        sys.exit("Aucun numéro de train reconnu dans le fichier.")
    res = G.importer_grille_officielle(a.date, trains, exhaustif=not a.non_exhaustif, note=f"import {os.path.basename(a.fichier)}")
    print(json.dumps(res, ensure_ascii=False))


def cmd_habituel(a):
    G = _g()
    G.definir_train_habituel(a.id, a.depart, a.arrivee, a.heure, numero_train=a.numero)
    print("Train habituel enregistré.")


def cmd_report(a):
    import bazo_temps_reel as TR
    TR.init_schema()
    res = TR.enregistrer_report_manuel(a.id, a.date, a.train1, a.train2, a.depart, a.arrivee, a.rupture, a.arrivee_reelle)
    print(json.dumps({k: v for k, v in res.items() if not k.endswith("_ts")}, ensure_ascii=False))


def cmd_repetition(a):
    os.environ["BAZO_SIMULATION"] = "1"
    import bazo_notify
    bazo_notify.SIMULATION = True
    import temps_reel_service as SVC
    r = SVC.cycle()
    print(json.dumps({k: v for k, v in r.items() if k != "changements"}, ensure_ascii=False, default=str))
    print(f"{len(bazo_notify.MESSAGES_SIMULES)} message(s) simulé(s), AUCUN envoyé.")
    for m in bazo_notify.MESSAGES_SIMULES[:5]:
        print("---", m["chat_id"], "\n" + m["texte"])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("statut"); s.add_argument("--date"); s.set_defaults(f=cmd_statut)
    s = sp.add_parser("forcer"); s.add_argument("--date", required=True)
    s.add_argument("--mode", default="force", choices=["force", "actif", "veille", "auto"]); s.add_argument("--note"); s.set_defaults(f=cmd_forcer)
    s = sp.add_parser("importer"); s.add_argument("--date", required=True); s.add_argument("--fichier", required=True)
    s.add_argument("--non-exhaustif", action="store_true"); s.set_defaults(f=cmd_importer)
    s = sp.add_parser("habituel"); s.add_argument("--id", required=True); s.add_argument("--depart", required=True)
    s.add_argument("--arrivee", required=True); s.add_argument("--heure", required=True); s.add_argument("--numero"); s.set_defaults(f=cmd_habituel)
    s = sp.add_parser("report"); s.add_argument("--id", required=True); s.add_argument("--date", required=True)
    s.add_argument("--train1", required=True); s.add_argument("--train2", required=True)
    s.add_argument("--depart", required=True); s.add_argument("--arrivee", required=True); s.add_argument("--rupture", required=True)
    s.add_argument("--arrivee-reelle", dest="arrivee_reelle"); s.set_defaults(f=cmd_report)
    s = sp.add_parser("repetition"); s.add_argument("--date"); s.set_defaults(f=cmd_repetition)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
