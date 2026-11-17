#!/usr/bin/env python3
"""Synchronise les séances de course du plan (index.html) vers le calendrier Garmin Connect.

- Lit SEANCES_BY_WEEK dans index.html et garde les séances structurées (champ « wo »)
  des 14 prochains jours, encore « à faire ».
- Crée chaque séance dans Garmin Connect (nom « Y2 jj/mm … ») et la programme à sa date.
- Ne touche qu'aux séances qu'il a lui-même créées (repère [y2:…] dans la description) :
  une séance future modifiée ou retirée du plan est supprimée puis recréée si besoin.
  Les séances passées et tout le reste du compte ne sont jamais modifiés.

Usage :
  python tools/garmin_sync.py --fingerprint   # empreinte des séances à synchroniser
  python tools/garmin_sync.py --dry-run       # affiche ce qui serait envoyé, sans connexion
  python tools/garmin_sync.py                 # synchronise (GARMIN_EMAIL / GARMIN_PASSWORD)
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
HORIZON = 14
MARK = re.compile(r"\[y2:(\d{4}-\d{2}-\d{2}):([0-9a-f]{10})\]")
RUN = {"sportTypeId": 1, "sportTypeKey": "running"}
TYP = {"wu": (1, "warmup"), "cd": (2, "cooldown"), "run": (3, "interval"), "rec": (4, "recovery")}


def today():
    return dt.datetime.now(ZoneInfo("Europe/Paris")).date()


def load_plan():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    i = html.index("const SEANCES_BY_WEEK=") + len("const SEANCES_BY_WEEK=")
    data, _ = json.JSONDecoder().raw_decode(html[i:])
    return data


def ascii_(t):
    t = str(t).replace("×", "x").replace("—", "-").replace("–", "-")
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\x20-\x7E]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def short_name(se):
    d = se["date"]
    t = ascii_(se["titre"])
    for a, b in [(r"^Club route - seuil", "Seuil"), (r"^Club route - ", "Club "), (r"^Club trail - ", "Trail "),
                 (r"^Sortie longue", "SL"), (r" a allure semi", " AS"), (r" - ", " ")]:
        t = re.sub(a, b, t, count=1)
    return f"Y2 {d[8:]}/{d[5:7]} {t}"[:48]


def key(se):
    raw = json.dumps([se["date"], se["titre"], se["wo"]], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def wanted(plan, ref=None):
    ref = ref or today()
    lim = ref + dt.timedelta(days=HORIZON)
    out = []
    for sessions in plan.values():
        for se in sessions:
            if not se.get("wo") or not se.get("date"):
                continue
            d = dt.date.fromisoformat(se["date"])
            statut = (se.get("realise") or {}).get("statut") or "a_faire"
            if ref <= d <= lim and statut == "a_faire":
                out.append(se)
    return sorted(out, key=lambda s: s["date"])


def step(st, order, child=None):
    t = TYP.get(st["k"], TYP["run"])
    o = {"type": "ExecutableStepDTO", "stepOrder": order, "stepType": {"stepTypeId": t[0], "stepTypeKey": t[1]},
         "description": st["n"]}
    if st.get("km"):
        o["endCondition"] = {"conditionTypeId": 3, "conditionTypeKey": "distance"}
        o["endConditionValue"] = round(st["km"] * 1000)
        o["preferredEndConditionUnit"] = {"unitKey": "kilometer"}
    else:
        o["endCondition"] = {"conditionTypeId": 2, "conditionTypeKey": "time"}
        o["endConditionValue"] = int(st["s"])
    if st.get("p"):  # allure lente / rapide en s/km -> vitesses en m/s
        o["targetType"] = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}
        o["targetValueOne"] = round(1000 / st["p"][0], 4)
        o["targetValueTwo"] = round(1000 / st["p"][1], 4)
    elif st.get("hr"):
        o["targetType"] = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}
        o["targetValueOne"], o["targetValueTwo"] = st["hr"]
    else:
        o["targetType"] = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
    if child:
        o["childStepId"] = child
    return o


def payload(se):
    order, grp, steps = 0, 0, []
    for st in se["wo"]:
        if st["k"] != "rep":
            order += 1
            steps.append(step(st, order))
            continue
        grp += 1
        order += 1
        g = {"type": "RepeatGroupDTO", "stepOrder": order, "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
             "childStepId": grp, "numberOfIterations": st["x"], "smartRepeat": False,
             "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"}, "endConditionValue": st["x"],
             "workoutSteps": []}
        for c in st["s"]:
            order += 1
            g["workoutSteps"].append(step(c, order, grp))
        steps.append(g)
    desc = ascii_(re.sub(r"<[^>]+>", "", se.get("objectif") or ""))[:400]
    return {"workoutName": short_name(se), "description": f"{desc}\n[y2:{se['date']}:{key(se)}]",
            "sportType": RUN, "workoutSegments": [{"segmentOrder": 1, "sportType": RUN, "workoutSteps": steps}]}


def fingerprint(ws):
    return hashlib.sha1("|".join(f"{s['date']}:{key(s)}" for s in ws).encode()).hexdigest()[:16]


def summary(lines):
    print("\n".join(lines))
    p = os.getenv("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main():
    ws = wanted(load_plan())
    if "--fingerprint" in sys.argv:
        print(fingerprint(ws))
        return 0
    if "--dry-run" in sys.argv:
        for se in ws:
            p = payload(se)
            print(se["date"], p["workoutName"], "|", len(p["workoutSegments"][0]["workoutSteps"]), "étapes")
        if "--json" in sys.argv and ws:
            print(json.dumps(payload(ws[0]), ensure_ascii=False, indent=1))
        return 0

    email, pwd = os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD")
    if not email or not pwd:
        summary(["## Synchro Garmin", "Secrets GARMIN_EMAIL / GARMIN_PASSWORD absents : synchro ignorée."])
        return 0
    from garminconnect import Garmin  # import tardif : inutile pour --dry-run

    api = Garmin(email, pwd, return_on_mfa=True)
    res = api.login()
    if isinstance(res, tuple) and res and res[0] == "needs_mfa":
        summary(["## Synchro Garmin : ÉCHEC", "Ton compte Garmin demande un code de validation (MFA) : "
                 "la synchro automatique ne peut pas le saisir. Voir les instructions dans le tableau de bord."])
        return 1

    ref = today()
    existing = []
    start = 0
    while True:
        page = api.get_workouts(start, 100, my_workouts_only=True) or []
        existing += page
        if len(page) < 100:
            break
        start += 100
    managed = [w for w in existing if str(w.get("workoutName", "")).startswith("Y2 ")]
    want = {f"{s['date']}:{key(s)}": s for s in ws}
    kept, removed, created, errors = 0, [], [], []
    for w in managed:
        try:
            full = api.get_workout_by_id(w["workoutId"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"lecture {w.get('workoutName')} : {e}")
            continue
        m = MARK.search(full.get("description") or "")
        if not m:
            continue  # séance « Y2 » créée à la main : on n'y touche pas
        d = dt.date.fromisoformat(m.group(1))
        if d < ref:
            continue  # séance passée : conservée telle quelle
        k = f"{m.group(1)}:{m.group(2)}"
        if k in want:
            want.pop(k)
            kept += 1
        else:
            api.delete_workout(w["workoutId"])
            removed.append(w.get("workoutName"))
    for k, se in want.items():
        try:
            r = api.upload_workout(payload(se))
            wid = r.get("workoutId")
            api.schedule_workout(wid, se["date"])
            created.append(f"{se['date']} · {short_name(se)}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{short_name(se)} : {e}")
    try:
        api.send_scheduled_workouts_to_devices()
    except Exception:  # noqa: BLE001
        pass
    cal = []
    try:
        lim = (ref + dt.timedelta(days=HORIZON)).isoformat()
        items = api.get_scheduled_workouts_by_period(ref.isoformat(), lim) or []
        if isinstance(items, dict):
            items = items.get("workoutScheduleList") or items.get("scheduledWorkouts") or list(items.values())
        for it in items:
            if not isinstance(it, dict):
                continue
            nm = it.get("title") or it.get("workoutName") or (it.get("workout") or {}).get("workoutName") or ""
            dd = str(it.get("date") or it.get("calendarDate") or it.get("scheduleDate") or "")[:10]
            if str(nm).startswith("Y2 "):
                cal.append(f"{dd} · {nm}")
        if not cal and items:
            cal.append("(format inattendu : " + ", ".join(sorted(items[0].keys()))[:300] + ")")
    except Exception as e:  # noqa: BLE001
        cal.append(f"(lecture du calendrier impossible : {e})")
    lines = ["## Synchro Garmin", f"- Séances à jour : {kept}", f"- Ajoutées : {len(created)}"]
    lines += [f"  - {c}" for c in created]
    lines += [f"- Retirées (modifiées ou supprimées du plan) : {len(removed)}"] + [f"  - {r}" for r in removed]
    lines += [f"- Vérification dans le calendrier Garmin ({HORIZON} jours) : {len([c for c in cal if not c.startswith('(')])} séance(s) Y2 programmée(s)"]
    lines += [f"  - {c}" for c in sorted(cal)]
    if errors:
        lines += ["- **Erreurs :**"] + [f"  - {e}" for e in errors]
    summary(lines)
    return 1 if errors and not created and not kept else 0


if __name__ == "__main__":
    sys.exit(main())
