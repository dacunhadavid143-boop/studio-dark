#!/usr/bin/env python3
"""
STUDIO DARK MOTIVATION — montage vidéo automatique calé sur une voix off
-----------------------------------------------------------------------
Entrée : un script (une phrase par ligne, FR ou EN) + la voix off (.mp3/.wav) du même nom.
1. Calage : repère le moment où chaque phrase est dite (silences de la voix + longueur des lignes).
2. Claude (Haiku) écrit un plan visuel par phrase et des requêtes de recherche en anglais.
3. Recherche des clips : banque perso (mes_clips), YouTube Creative Commons, Internet Archive
   (+ Pexels / Pixabay si clés).
4. Claude (Sonnet, vision) trie les vignettes, choisit le meilleur moment et rejette logos/texte.
5. Montage ffmpeg 9:16 sombre (noir et blanc, grain, vignettage, zooms), à la durée exacte de la voix.
Sortie : vidéo SANS son ni texte (voix, musique et sous-titres sont ajoutés ensuite).

Lancement :  python studio.py
Options   :  python studio.py --format long --script caesar.txt --theme "rome, legion" --oui
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*a, **k):
        return False

# --------------------------------------------------------------------------
# Réglages
# --------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

W, H, FPS = 1080, 1920, 30
MODEL_PLAN = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")        # liste des plans (texte simple)
MODEL_TRI = os.getenv("CLAUDE_MODEL_TRI", "claude-sonnet-5-5")    # tri des clips (vision, plus exigeant)
PLANS_EN_PARALLELE = int(os.getenv("PLANS_EN_PARALLELE", "3"))

# Chaînes YouTube écartées (watermark incrusté dans leurs vidéos). Ajoute les tiennes dans .env :
# CHAINES_BANNIES=nom1,nom2
CHAINES_BANNIES = {"videvo"} | {c.strip().lower() for c in os.getenv("CHAINES_BANNIES", "").split(",") if c.strip()}
# Titres écartés d'office : tutoriels, vlogs, interviews... (rarement du bon b-roll)
MOTS_BANNIS = re.compile(
    r"\b(tuto\w*|tutorial|how to|how-to|explained|lesson|tips|guide|review|reaction|vlog|interview|podcast|"
    r"webinar|unboxing|lyrics?|music video|full movie|live ?stream|first aid|course|class|q&a|news|"
    r"comment|astuce|cours)\b", re.I)

# Les gros fichiers (banque de clips, vidéos finies, fichiers de travail) peuvent vivre sur un autre disque :
# ajoute DOSSIER_DONNEES=D:\studio_dark_data dans .env
DATA = Path(os.getenv("DOSSIER_DONNEES", "").strip() or BASE)
D_CLIPS = DATA / "mes_clips"      # tes propres clips / ta banque de scènes
D_SORTIES = DATA / "sorties"      # vidéos finies
D_CACHE = DATA / "cache"          # fichiers de travail (nettoyés après chaque vidéo)
D_SCRIPTS = BASE / "scripts"     # tes scripts de voix off (.txt ou .srt)
HISTORIQUE = BASE / "historique.json"

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")

FORMATS = {
    # Deux formats, même principe : ta voix off + ton script -> le studio trouve les images.
    # La durée exacte vient toujours de ta voix off.
    "long": dict(nom="Long — storytelling (50 s à 1 min 10)", duree=(50, 70), plans=(12, 22), look="hebdo",
                 consigne="Vraie histoire racontée : situation, chute, solitude, déclic, travail acharné, revanche. "
                          "Ambiance immersive et posée : plans de 2,5 à 5 s qui respirent, \"coupes\": 1 presque partout "
                          "(2 au maximum sur le climax). Le tout premier plan peut être en couleur (\"couleur\": true), "
                          "puis tout bascule en noir et blanc."),
    "court": dict(nom="Court — punch (6 à 8 s)", duree=(6, 8), plans=(3, 5), look="soir",
                  consigne="DEUX ACTES. Acte 1 (\"acte\": 1, environ la première moitié) : plans lents, très sombres "
                           "(silhouette seule qui marche, nuit, brouillard). Acte 2 (\"acte\": 2) : bascule brutale "
                           "vers du combat intense (boxe, MMA, KO, regard de champion), avec \"coupes\": 2."),
}

LOOKS = {
    # nb = noir et blanc ; teinte = teinte froide pour la couleur ; ca = aberration chromatique (px)
    # glitch = lignes noires sur les premières images d'un plan ; pousse = zoom lent sur tout le plan
    # impact = zoom d'impact au début du plan ; noir = écrasement des noirs (0 = rien, 1 = fort)
    "rapide": dict(contrast=1.25, noir=0.8, vignette=3.3, grain=10, ca=3, glitch=0.6, pousse=0.04,
                   impact=0.06, impact_dur=0.18, flash=0.0),
    "pose": dict(contrast=1.15, noir=1.0, vignette=3.0, grain=9, ca=0, glitch=0.0, pousse=0.06,
                 impact=0.0, impact_dur=0.5, flash=0.0, fondu=0.4),
    "soir": dict(contrast=1.3, noir=0.9, vignette=3.2, grain=8, ca=2, glitch=0.0, pousse=0.05,
                 impact=0.12, impact_dur=0.22, flash=0.15),
    "hebdo": dict(contrast=1.2, noir=0.7, vignette=2.7, grain=10, ca=0, glitch=0.0, pousse=0.05,
                  impact=0.0, impact_dur=0.5, flash=0.0, fondu=0.25),
}
RALENTI = 1.6  # facteur de ralenti quand un plan est marqué "ralenti"

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

# Suivi des jetons Claude (pour voir ce que coûte chaque vidéo)
USAGE = {"in": 0, "out": 0, "appels": 0}
T0 = time.time()
_LOCK = threading.Lock()


_PRINT = threading.Lock()


def log(*a):
    with _PRINT:
        print(*a, flush=True)


def stop(msg):
    log("\n❌ " + msg)
    sys.exit(1)


def slug(s, n=40):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:n] or "video"


def run(cmd, cwd=None, timeout=None):
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("ffmpeg a échoué :\n" + r.stderr[-2000:])
    return r.stdout


def probe_duration(src):
    try:
        out = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", src], timeout=60)
        return float(out.strip())
    except Exception:
        return None


# --------------------------------------------------------------------------
# Historique (pour ne jamais réutiliser deux fois le même clip)
# --------------------------------------------------------------------------
def load_hist():
    try:
        return json.loads(HISTORIQUE.read_text(encoding="utf-8"))
    except Exception:
        return {"utilises": {}}


def save_hist(h):
    HISTORIQUE.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------
_client = None

SYSTEM = (
    "Tu es le monteur d'un compte TikTok de dark motivation (scripts en anglais ou en français) "
    "(sport de combat, discipline, solitude, revanche, mental). Ambiance sombre, nocturne, "
    "brute, jamais joyeuse ni colorée. Tu comprends le script dans sa langue d'origine, mais tu écris "
    "toujours tes descriptions et explications en français pour l'utilisateur. "
    "Tu réponds toujours en JSON valide quand on te le demande."
)


def claude():
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            stop("Clé ANTHROPIC_API_KEY manquante dans le fichier .env (voir LISEZMOI.md, étape 3).")
        try:
            import anthropic
        except ImportError:
            stop("Le module 'anthropic' n'est pas installé. Lance : pip install -r requirements.txt")
        ws = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
        _client = anthropic.Anthropic(api_key=key,
                                      default_headers={"anthropic-workspace-id": ws} if ws else None)
    return _client


def ask(content, max_tokens=3000, model=None):
    max_tokens = max(max_tokens, 2500)  # les modèles récents réfléchissent avant de répondre
    for essai in range(3):
        try:
            msg = claude().messages.create(model=model or MODEL_PLAN, max_tokens=max_tokens, system=SYSTEM,
                                           messages=[{"role": "user", "content": content}])
            with _LOCK:
                USAGE["in"] += msg.usage.input_tokens
                USAGE["out"] += msg.usage.output_tokens
                USAGE["appels"] += 1
            txt = "".join(getattr(b, "text", "") or "" for b in msg.content if getattr(b, "type", "") == "text")
            raison = getattr(msg, "stop_reason", "?")
            if raison == "max_tokens" and essai < 2:  # réponse coupée en plein milieu : on redemande avec plus de place
                max_tokens *= 2
                continue
            if not txt.strip():
                log(f"   (réponse vide de Claude, raison : {raison})")
            return txt
        except Exception as e:  # surcharge / réseau : on réessaie
            if essai == 2:
                raise
            log(f"   (Claude indisponible, nouvel essai dans 5 s : {str(e)[:80]})")
            time.sleep(5)
    return ""


def ask_json(content, max_tokens=3000, model=None):
    txt = ""
    for _ in range(2):
        txt = ask(content, max_tokens, model)
        m = re.search(r"\{.*\}", txt or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        rappel = "\n\nIMPORTANT : réponds uniquement avec un objet JSON valide."
        if isinstance(content, str):
            content = content + rappel
        else:
            content = content[:-1] + [{"type": "text", "text": content[-1]["text"] + rappel}]
    raise RuntimeError("Claude n'a pas renvoyé de JSON lisible :\n" + (txt or "(réponse vide)")[:500])


def img_block(path):
    data = base64.b64encode(Path(path).read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


# --------------------------------------------------------------------------
# 1. Le plan de la vidéo
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 1 bis. Mode script : un plan par phrase, calé sur le moment où elle est dite
# --------------------------------------------------------------------------
MOTS_PAR_SECONDE = float(os.getenv("MOTS_PAR_SECONDE", "2.5"))  # débit d'une voix off dark motivation
PAUSE_PHRASE = 0.35


def _srt_time(t):
    h, m, rest = t.strip().replace(".", ",").split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


def silences(audio):
    """Liste des silences [(début, fin)] de la voix off, détectés par ffmpeg."""
    r = subprocess.run([FFMPEG, "-i", str(audio), "-af", "silencedetect=noise=-34dB:d=0.15", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
    debuts = [float(x) for x in re.findall(r"silence_start: (-?[0-9.]+)", r.stderr)]
    fins = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", r.stderr)]
    total = probe_duration(str(audio)) or 0
    out = []
    for k, d in enumerate(debuts):
        out.append((max(0.0, d), fins[k] if k < len(fins) else total))
    return out, total


def caler_sur_voix(lignes, audio):
    """Trouve le moment où commence chaque ligne du script dans la voix off.
    On combine la longueur de chaque ligne (nombre de mots) et les silences de la voix :
    une pause dramatique au milieu d'une phrase n'est pas prise pour un changement de ligne."""
    sil, total = silences(audio)
    debut_voix = sil[0][1] if sil and sil[0][0] <= 0.05 else 0.0
    fin_voix = sil[-1][0] if sil and sil[-1][1] >= total - 0.05 else total
    fin_voix = max(fin_voix, debut_voix + 1)
    # pauses candidates (milieu du silence, et sa longueur)
    cands = [((a + b) / 2, b - a, b) for a, b in sil if a > debut_voix + 0.05 and b < fin_voix - 0.05]
    mots = [max(1, len(l.split())) for l in lignes]
    tot_mots, cumul, attendus = sum(mots), 0, []
    for m in mots[:-1]:
        cumul += m
        attendus.append(debut_voix + (fin_voix - debut_voix) * cumul / tot_mots)
    # alignement optimal (programmation dynamique) : chaque changement de ligne tombe dans une pause,
    # au plus près de l'endroit attendu, en préférant les pauses longues
    n, m = len(attendus), len(cands)
    INF = float("inf")
    if n == 0:
        return [{"texte": lignes[0], "debut": debut_voix, "fin": fin_voix}], total
    # cout[i][j] : meilleur coût pour placer les i premiers changements avec le i-ème sur la pause j
    cout = [[INF] * m for _ in range(n)]
    prec = [[-1] * m for _ in range(n)]
    span = (fin_voix - debut_voix)

    def c(i, j):
        ecart = abs(cands[j][0] - attendus[i]) / span
        return ecart * 10 - min(cands[j][1], 1.2) * 1.5

    for j in range(m):
        cout[0][j] = c(0, j)
    for i in range(1, n):
        meilleur, arg = INF, -1
        for j in range(m):
            if j > 0 and cout[i - 1][j - 1] < meilleur:
                meilleur, arg = cout[i - 1][j - 1], j - 1
            if meilleur < INF:
                cout[i][j] = meilleur + c(i, j)
                prec[i][j] = arg
    debuts = None
    if m >= n:
        j = min(range(m), key=lambda j: cout[n - 1][j])
        if cout[n - 1][j] < INF:
            choix = []
            for i in range(n - 1, -1, -1):
                choix.append(j)
                j = prec[i][j]
            choix.reverse()
            debuts = [cands[j][2] for j in choix]  # la ligne commence à la fin de la pause
    if debuts is None:  # pas assez de pauses : on s'appuie sur la longueur des lignes
        debuts = attendus
    debuts = [debut_voix] + debuts
    segs = []
    for k, l in enumerate(lignes):
        fin = debuts[k + 1] if k + 1 < len(debuts) else fin_voix
        segs.append({"texte": l, "debut": debuts[k], "fin": fin})
    return segs, total


def lire_script(path):
    """Renvoie [{"texte", "debut", "fin"}].
    .srt (exporté depuis CapCut, Edits...) -> timings exacts de ta voix off.
    .txt -> une phrase par ligne, timings estimés d'après le nombre de mots."""
    brut = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    segs = []
    if Path(path).suffix.lower() == ".srt":
        for bloc in re.split(r"\n\s*\n", brut.strip()):
            lignes = [l for l in bloc.strip().splitlines() if l.strip()]
            for k, l in enumerate(lignes):
                m = re.match(r"\s*([\d:,.]+)\s*-->\s*([\d:,.]+)", l)
                if m:
                    texte = " ".join(lignes[k + 1:]).strip()
                    if texte:
                        segs.append({"texte": re.sub(r"<[^>]+>", "", texte),
                                     "debut": _srt_time(m.group(1)), "fin": _srt_time(m.group(2))})
                    break
    else:
        lignes = [l.strip() for l in brut.splitlines() if l.strip() and not l.strip().startswith("#")]
        audio = next((Path(path).with_suffix(e) for e in AUDIO_EXT if Path(path).with_suffix(e).exists()), None)
        if audio and lignes:
            segs, total = caler_sur_voix(lignes, audio)
            segs[-1]["fin"] = max(segs[-1]["fin"], total)
            segs[0]["audio"] = str(audio)
            return segs
        t = 0.0
        for l in lignes:
            d = max(0.8, len(l.split()) / MOTS_PAR_SECONDE)
            segs.append({"texte": l, "debut": t, "fin": t + d})
            t += d + PAUSE_PHRASE
    return segs


def regrouper_segments(segs, mini, maxi):
    """Ajuste les phrases en plans : fusionne les sous-titres trop courts (mot par mot),
    coupe les phrases trop longues. Chaque plan couvre le temps jusqu'au plan suivant."""
    plans, cur = [], None
    for s in segs:
        if cur and (s["debut"] - cur["debut"] < mini):
            cur["texte"] += " " + s["texte"]
            cur["fin"] = s["fin"]
        else:
            if cur:
                plans.append(cur)
            cur = dict(s)
    if cur:
        plans.append(cur)
    for k, p in enumerate(plans):  # le plan dure jusqu'au début de la phrase suivante (pas de trou noir)
        p["fin"] = plans[k + 1]["debut"] if k + 1 < len(plans) else p["fin"] + (0.0 if p.get("audio") or plans[0].get("audio") else 0.6)
        p["debut"] = 0.0 if k == 0 else p["debut"]
    out = []
    for p in plans:
        d = p["fin"] - p["debut"]
        n = max(1, math.ceil(d / maxi))
        for j in range(n):
            out.append({"texte": p["texte"], "debut": p["debut"] + d * j / n, "duree": round(d / n, 3),
                        "suite": j > 0})
    return out


def plan_depuis_script(theme, fmt, segs):
    f = FORMATS[fmt]
    mini, maxi = {"soir": (1.0, 2.5), "hebdo": (2.0, 5.0)}[f["look"]]
    blocs = regrouper_segments(segs, mini, maxi)
    liste = "\n".join(f"{k + 1}. [{b['debut']:.1f}s, {b['duree']:.1f}s] "
                      + ("(suite de la phrase précédente, montre une autre image du même moment) " if b["suite"] else "")
                      + f"« {b['texte']} »" for k, b in enumerate(blocs))
    prompt = f"""Prépare les plans d'une vidéo TikTok verticale (montage visuel uniquement : l'utilisateur pose
lui-même sa voix off et son texte par-dessus).
Format : {f['nom']}. Consigne de style : {f['consigne']}
Thème / mots-clés de l'utilisateur : {theme}

Voici le script (il peut être en anglais : comprends-le tel quel, avec ses nuances), découpé en
{len(blocs)} plans avec le moment où chaque phrase est dite :
{liste}

Pour CHAQUE plan, dans le même ordre, décris l'image qui illustre le plus concrètement ce qui est dit À CE
MOMENT-LÀ (ex : « un homme de 19 ans qui marche seul » -> un jeune homme seul qui marche de dos, la nuit).
RÈGLE D'ÉPOQUE : si le script raconte une histoire historique (Rome antique, César, guerriers, Moyen Âge...),
les plans narratifs doivent rester dans l'époque : reconstitutions, films d'époque, légionnaires, armures,
chevaux, feu, navires anciens, statues, peintures, ruines (requêtes du type "roman legion reenactment",
"ancient ships burning", "roman soldier cinematic", "medieval army march"). Pas de ville moderne, de voiture
ni de téléphone sur ces phrases. La morale finale (« most people fail... ») peut revenir au monde actuel
(entraînement, effort, combat).
RÈGLE IMPORTANTE : quand une phrase contient une image concrète (roc, vagues, tempête, armure, ville, sang,
sueur, épée...), montre CETTE image sur CETTE phrase, et ne la « gaspille » pas sur une phrase précédente :
lis tout le script avant de répartir les images. Quand la phrase est abstraite (« la douleur forge le mental »),
traduis-la en image d'effort ou de solitude cohérente avec le thème. Garde une continuité visuelle d'un plan
à l'autre. Pour la toute dernière phrase, choisis une image forte (combat, silhouette en contre-jour, regard).
Requêtes de recherche EN ANGLAIS, 2 à 4 par plan, courtes (2-4 mots), concrètes et visuelles ; ajoute souvent
"stock footage", "cinematic" ou "b-roll". "ralenti": true pour 1 à 3 moments forts.
Tout sera étalonné en noir et blanc très sombre, sauf "couleur": true. Privilégie silhouettes, de dos,
de loin, contre-jour, nuit, pluie, brouillard, effort. Évite les visages qui parlent.

Réponds UNIQUEMENT avec ce JSON, exactement {len(blocs)} plans :
{{"titre": "titre court",
  "plans": [{{"description": "ce qu'on voit (en français)", "traduction": "traduction française de la phrase",
             "recherche": ["...", "..."], "coupes": 1, "acte": 1, "ralenti": false, "couleur": false}}]}}
"titre", "description" et "traduction" toujours EN FRANÇAIS (si la phrase est déjà en français, recopie-la)."""
    plan = ask_json(prompt, max_tokens=10000)
    recus = plan.get("plans") or []
    plans = []
    for k, b in enumerate(blocs):
        p = dict(recus[k]) if k < len(recus) else {"description": b["texte"], "recherche": [theme]}
        p["recherche"] = [q for q in (p.get("recherche") or []) if q][:4] or [theme]
        p["description"] = (p.get("description") or b["texte"]) + f" (phrase dite : « {b['texte']} »)"
        p["duree"] = b["duree"]  # le timing vient du script, pas de Claude
        p["phrase"] = b["texte"]
        tr = (p.get("traduction") or "").strip()
        p["traduction"] = tr if tr and tr.lower() != b["texte"].lower() else ""
        p["debut_script"] = round(b["debut"], 2)
        try:
            p["coupes"] = max(1, min(4, int(p.get("coupes") or 1)))
        except (TypeError, ValueError):
            p["coupes"] = 1
        if f["look"] == "soir":
            p["acte"] = 2 if str(p.get("acte")) == "2" else 1
        plans.append(p)
    plan["plans"] = plans
    plan["sync"] = True
    return plan


def plan_video(theme, fmt, duree=None):
    f = FORMATS[fmt]
    a, b = f["duree"]
    p1, p2 = f["plans"]
    if duree:
        a = b = duree
        par_plan = {"soir": 2.2, "hebdo": 4.0}[f["look"]]
        p1 = max(2, round(duree / par_plan))
        p2 = p1 + 2
    prompt = f"""Prépare la liste des plans d'une vidéo TikTok verticale (montage visuel uniquement,
l'utilisateur ajoute lui-même texte, musique et voix).
Format : {f['nom']} — durée totale {a}-{b} s, entre {p1} et {p2} plans.
Consigne du format : {f['consigne']}
Thème / idée de l'utilisateur : {theme}

Pour chaque plan, donne des requêtes de recherche EN ANGLAIS pour des banques vidéo (Pexels, Pixabay,
YouTube, Internet Archive) : 2 à 4 requêtes courtes (2-4 mots), concrètes et visuelles
(ex : "boxer heavy bag dark", "man running rain night", "fighter wrapping hands", "old boxing match 1950").
Ajoute souvent "stock footage", "cinematic" ou "b-roll" pour tomber sur des plans sans personne qui parle.
Pas de concepts abstraits. Varie les requêtes d'un plan à l'autre pour avoir des images différentes.
"ralenti": true pour 1 à 3 moments forts. Tout sera étalonné en noir et blanc très sombre, sauf indication
"couleur": true. Privilégie : silhouettes, de dos, de loin, contre-jour, nuit, pluie, brouillard, effort.
Évite les visages qui parlent et les scènes joyeuses.
La somme des "duree" doit tomber dans la durée totale.

Réponds UNIQUEMENT avec ce JSON :
{{"titre": "titre court",
  "plans": [{{"description": "ce qu'on voit", "recherche": ["...", "..."], "duree": 2.5,
             "coupes": 1, "acte": 1, "ralenti": false, "couleur": false}}]}}"""
    plan = ask_json(prompt, max_tokens=8000)
    plans = [p for p in plan.get("plans", []) if p.get("recherche") or p.get("description")]
    if not plans:
        raise RuntimeError("Le plan renvoyé est vide.")
    for p in plans:
        p["duree"] = max(0.4, float(p.get("duree") or 2))
        p["recherche"] = [q for q in (p.get("recherche") or []) if q][:4] or [p.get("description", "")]
        try:
            p["coupes"] = max(1, min(5, int(p.get("coupes") or 1)))
        except (TypeError, ValueError):
            p["coupes"] = 1
        if f["look"] == "soir":
            p["acte"] = 2 if str(p.get("acte")) == "2" else 1
    plan["plans"] = plans
    total = sum(p["duree"] for p in plans)
    cible = min(max(total, a), b)
    for p in plans:
        p["duree"] = round(p["duree"] * cible / total, 2)
    return plan


# --------------------------------------------------------------------------
# 2. Les sources de clips
# --------------------------------------------------------------------------
UA = {"User-Agent": "StudioDarkMotivation/1.0"}


def _cand(**k):
    k.setdefault("duree", None)
    k.setdefault("stream", None)
    k.setdefault("auteur", "")
    return k


def search_pexels(q, n=6):
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        return []
    r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": key},
                     params={"query": q, "per_page": n}, timeout=20)
    r.raise_for_status()
    out = []
    for v in r.json().get("videos", []):
        files = [f for f in v.get("video_files", []) if f.get("link") and f.get("width")]
        if not files:
            continue
        # le plus petit fichier qui garde au moins 1080 px sur le petit côté, sinon le plus grand
        bons = [f for f in files if min(f["width"], f["height"]) >= 1080]
        f = min(bons, key=lambda f: f["width"] * f["height"]) if bons else \
            max(files, key=lambda f: f["width"] * f["height"])
        out.append(_cand(id=f"pexels:{v['id']}", source="Pexels", thumb=v.get("image"),
                         stream=f["link"], duree=v.get("duration"), page=v.get("url"),
                         auteur=(v.get("user") or {}).get("name", ""), licence="Licence Pexels (libre)"))
    return out


def search_pixabay(q, n=6):
    key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not key:
        return []
    r = requests.get("https://pixabay.com/api/videos/",
                     params={"key": key, "q": q[:100], "per_page": max(3, n), "safesearch": "true"},
                     timeout=20)
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", []):
        vids = h.get("videos", {})
        best = None
        for k in ("large", "medium", "small"):
            if vids.get(k, {}).get("url"):
                best = vids[k]
                break
        if not best:
            continue
        thumb = (vids.get("medium") or {}).get("thumbnail") or best.get("thumbnail")
        out.append(_cand(id=f"pixabay:{h['id']}", source="Pixabay", thumb=thumb, stream=best["url"],
                         duree=h.get("duration"), page=h.get("pageURL"), auteur=h.get("user", ""),
                         licence="Licence Pixabay (libre)"))
    return out


def _iso_duration(s):
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def search_youtube(q, n=6):
    """Uniquement les vidéos publiées sous licence Creative Commons (vérifiée vidéo par vidéo)."""
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key:
        return []
    r = requests.get("https://www.googleapis.com/youtube/v3/search", timeout=20, params=dict(
        part="snippet", q=f"{q} -tutorial -vlog -review -interview -podcast", type="video",
        videoLicense="creativeCommon", videoDuration=os.getenv("YOUTUBE_DUREE", "any"), maxResults=n, key=key))
    if r.status_code == 403:
        log("   (quota YouTube du jour atteint, YouTube ignoré)")
        return []
    r.raise_for_status()
    ids = [it["id"]["videoId"] for it in r.json().get("items", []) if it.get("id", {}).get("videoId")]
    if not ids:
        return []
    r2 = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=20, params=dict(
        part="snippet,contentDetails,status", id=",".join(ids), key=key))
    r2.raise_for_status()
    out = []
    for v in r2.json().get("items", []):
        if (v.get("status") or {}).get("license") != "creativeCommon":
            continue
        dur = _iso_duration(v["contentDetails"].get("duration"))
        if not dur or dur > 20 * 60:
            continue
        titre, chaine = v["snippet"].get("title", ""), v["snippet"].get("channelTitle", "")
        if (MOTS_BANNIS.search(titre) or any(b in chaine.lower() for b in CHAINES_BANNIES)
                or re.search(r"\b(news|tv|actualit|journal)\b", chaine, re.I)):
            continue
        th = v["snippet"].get("thumbnails", {})
        thumb = (th.get("high") or th.get("medium") or th.get("default") or {}).get("url")
        out.append(_cand(id=f"youtube:{v['id']}", source="YouTube CC", thumb=thumb, duree=dur,
                         page=f"https://www.youtube.com/watch?v={v['id']}",
                         auteur=v["snippet"].get("channelTitle", ""),
                         titre=v["snippet"].get("title", ""),
                         licence="Creative Commons BY (YouTube) — crédit obligatoire"))
    return out


def search_archive(q, n=4):
    query = (f"({q}) AND mediatype:(movies) AND "
             f"(collection:(prelinger) OR licenseurl:(*publicdomain*) OR licenseurl:(*creativecommons*))")
    r = requests.get("https://archive.org/advancedsearch.php", headers=UA, timeout=25, params={
        "q": query, "fl[]": ["identifier", "title", "licenseurl", "creator"], "rows": n, "output": "json"})
    r.raise_for_status()
    out = []
    for d in r.json().get("response", {}).get("docs", []):
        lic = d.get("licenseurl") or "Domaine public (Prelinger Archives)"
        if isinstance(lic, list):
            lic = lic[0]
        if "/nd" in lic.lower() or "-nd" in lic.lower():  # pas de modification autorisée -> on évite
            continue
        ident = d["identifier"]
        creator = d.get("creator") or ""
        if isinstance(creator, list):
            creator = creator[0]
        out.append(_cand(id=f"archive:{ident}", source="Internet Archive",
                         thumb=f"https://archive.org/services/img/{ident}",
                         page=f"https://archive.org/details/{ident}", auteur=creator, licence=lic,
                         titre=d.get("title", "")))
    return out


def list_local_clips():
    if not D_CLIPS.exists():
        return []
    return sorted(p for p in D_CLIPS.rglob("*") if p.suffix.lower() in VIDEO_EXT)


MOTS_VIDES = {"the", "and", "with", "for", "from", "dark", "night", "man", "stock", "footage", "cinematic",
              "roll", "slow", "motion", "video", "scene", "des", "les", "une", "dans", "sur"}


def search_local(shot, exclus, n=6):
    """Cherche dans ta banque mes_clips : les noms de fichiers servent de mots-clés."""
    clips = [c for c in list_local_clips() if f"perso:{c.name}" not in exclus]
    if not clips:
        return []
    texte = (shot.get("description", "") + " " + " ".join(shot.get("recherche", []))).lower()
    mots = {m for m in re.findall(r"[a-zà-ÿ]{3,}", texte) if m not in MOTS_VIDES}
    mots |= {m[:-1] for m in mots if m.endswith("s")}  # pluriels : ships -> ship

    def score(p):
        nom = set(re.findall(r"[a-zà-ÿ]{3,}", p.stem.lower().replace("_", " ")))
        nom |= {m[:-1] for m in nom if m.endswith("s")}
        return len(nom & mots)

    notes = [(score(c), random.random(), c) for c in clips]
    notes = [x for x in notes if x[0] >= 1]  # au moins un mot-clé en commun
    notes.sort(reverse=True)
    return [_cand(id=f"perso:{c.name}", source="Mes clips", thumb=None, stream=str(c),
                  page=c.name, licence="Clip perso", titre=c.stem) for _, _, c in notes[:n]]


# --------------------------------------------------------------------------
# Accès aux flux vidéo (pour les sources qui ne donnent pas de lien direct)
# --------------------------------------------------------------------------
def archive_stream(ident):
    j = requests.get(f"https://archive.org/metadata/{ident}", headers=UA, timeout=25).json()
    files = [f for f in j.get("files", []) if f.get("name", "").lower().endswith((".mp4", ".m4v", ".mov", ".ogv", ".mpeg"))]
    if not files:
        return None, None

    def rang(f):
        fm = (f.get("format") or "").lower()
        return (("hd" in fm) * 3 + ("h.264" in fm) * 2 + f["name"].lower().endswith(".mp4"),
                int(f.get("size") or 0))

    f = max(files, key=rang)
    dur = None
    try:
        L = str(f.get("length") or "")
        if ":" in L:
            parts = [float(x) for x in L.split(":")]
            dur = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
        elif L:
            dur = float(L)
    except ValueError:
        pass
    return f"https://archive.org/download/{ident}/{quote(f['name'])}", dur


YT_FORMAT = "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b[height<=1080]/b"
YT_APERCU = "bv*[height<=360][ext=mp4]/bv*[height<=360]/b[height<=360]/worst"


def youtube_stream(url, fmt=YT_FORMAT):
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": fmt}) as y:
        info = y.extract_info(url, download=False)
    stream = info.get("url") or (info.get("requested_formats") or [{}])[0].get("url")
    return stream, info.get("duration")


def youtube_download(url, vid):
    import yt_dlp
    D_CACHE.mkdir(parents=True, exist_ok=True)
    out = D_CACHE / f"yt_{vid}.mp4"
    if out.exists():
        return str(out)
    opts = {"quiet": True, "no_warnings": True, "format": YT_FORMAT,
            "outtmpl": str(D_CACHE / f"yt_{vid}.%(ext)s"), "merge_output_format": "mp4"}
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([url])
    for p in D_CACHE.glob(f"yt_{vid}.*"):
        return str(p)
    return None


def resolve_apercu(c):
    """Flux basse qualité, rapide, pour extraire les images que Claude regarde."""
    if c["source"] == "YouTube CC":
        if not c.get("apercu"):
            try:
                c["apercu"], d = youtube_stream(c["page"], YT_APERCU)
                c["duree"] = c["duree"] or d
            except Exception:
                c["apercu"] = None
        if c.get("apercu"):
            return c["apercu"], c["duree"]
    return resolve(c)


def resolve(c):
    """Renvoie (chemin ou URL pleine qualité lisible par ffmpeg, durée)."""
    if c["source"] == "Internet Archive" and not c["stream"]:
        c["stream"], d = archive_stream(c["id"].split(":", 1)[1])
        c["duree"] = c["duree"] or d
    elif c["source"] == "YouTube CC" and not c["stream"]:
        try:
            c["stream"], d = youtube_stream(c["page"])
        except Exception:
            c["stream"], d = None, None
        if not c["stream"]:
            c["stream"] = youtube_download(c["page"], c["id"].split(":", 1)[1])
        c["duree"] = c["duree"] or d
    if c["stream"] and not c["duree"]:
        c["duree"] = probe_duration(c["stream"])
    return c["stream"], c["duree"]


# --------------------------------------------------------------------------
# Images : vignettes, extraction d'images, planches pour Claude
# --------------------------------------------------------------------------
def load_font(size):
    for f in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "Arial.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def cover(im, w, h):
    s = max(w / im.width, h / im.height)
    im = im.resize((max(w, math.ceil(im.width * s)), max(h, math.ceil(im.height * s))))
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def grab_frame(src, t, out, w=270, h=480):
    cmd = [FFMPEG, "-y", "-v", "error", "-ss", f"{max(0, t):.2f}", "-i", src, "-frames:v", "1",
           "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}", "-q:v", "4", out]
    try:
        subprocess.run([str(c) for c in cmd], capture_output=True, timeout=90)
    except subprocess.TimeoutExpired:
        return False
    return Path(out).exists() and Path(out).stat().st_size > 0


def img_stats(path):
    """(luminosité moyenne 0-255, netteté, luminosité des zones claires) — pour écarter les images
    trop claires, floues, ou au contraire noires (rien de visible une fois étalonnées)."""
    try:
        g = Image.open(path).convert("L")
        lum = ImageStat.Stat(g).mean[0]
        net = ImageStat.Stat(g.resize((180, 320)).filter(ImageFilter.FIND_EDGES)).stddev[0]
        hist = g.histogram()
        total, acc, p95 = sum(hist), 0, 0
        for v, n in enumerate(hist):
            acc += n
            if acc >= 0.95 * total:
                p95 = v
                break
        return lum, net, p95
    except Exception:
        return 0, 99, 255


def image_valable(path, lum_max=150, net_min=5, lum_min=22, clair_min=85):
    lum, net, p95 = img_stats(path)
    return lum_min <= lum <= lum_max and net >= net_min and p95 >= clair_min


def fetch_thumb(c, workdir):
    out = workdir / "thumbs" / (hashlib.md5(c["id"].encode()).hexdigest() + ".jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return str(out)
    try:
        if c["source"] == "Mes clips":
            d = probe_duration(c["stream"]) or 4
            c["duree"] = d
            return str(out) if grab_frame(c["stream"], d * 0.4, str(out)) else None
        r = requests.get(c["thumb"], headers=UA, timeout=20)
        r.raise_for_status()
        out.write_bytes(r.content)
        Image.open(out).convert("RGB").save(out, "JPEG", quality=85)
        return str(out)
    except Exception:
        return None


def make_grid(items, out, cols=4, cw=240, ch=427):
    """items = [(chemin_image, étiquette)] -> planche numérotée, recadrée en 9:16 comme le rendu final."""
    rows = math.ceil(len(items) / cols)
    img = Image.new("RGB", (cols * cw, rows * ch), (15, 15, 15))
    d = ImageDraw.Draw(img)
    font = load_font(26)
    for i, (path, label) in enumerate(items):
        x, y = (i % cols) * cw, (i // cols) * ch
        try:
            im = cover(Image.open(path).convert("RGB"), cw, ch)
            img.paste(im, (x, y))
        except Exception:
            pass
        d.rectangle([x, y, x + cw - 1, y + ch - 1], outline=(0, 0, 0), width=4)
        d.text((x + 10, y + 8), label, fill=(255, 230, 0), font=font, stroke_width=3, stroke_fill=(0, 0, 0))
    img.save(out, "JPEG", quality=82)
    return out


# --------------------------------------------------------------------------
# 3. Trouver le bon clip pour chaque plan
# --------------------------------------------------------------------------
def gather_candidates(shot, exclus):
    """Lance toutes les recherches en parallèle et mélange les sources."""
    queries = shot["recherche"]
    jobs = []
    for i, q in enumerate(queries):
        jobs.append((search_pexels, q, 5))
        jobs.append((search_pixabay, q, 5))
        if i == 0:  # YouTube coûte du quota -> une seule requête par plan
            jobs.append((search_youtube, q, 6))
        if i < 2:
            jobs.append((search_archive, q, 3))

    def safe(job):
        fn, q, n = job
        try:
            return fn(q, n)
        except Exception as e:
            log(f"   ({fn.__name__.replace('search_', '')} : {str(e)[:70]})")
            return []

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(safe, jobs))
    par_source, vus = {}, set()
    for res in results:
        for c in res:
            if c["id"] in vus or c["id"] in exclus:
                continue
            vus.add(c["id"])
            par_source.setdefault(c["source"], []).append(c)
    # on alterne les sources pour avoir de la variété
    melange = []
    while any(par_source.values()) and len(melange) < 16:
        for s in list(par_source):
            if par_source[s]:
                melange.append(par_source[s].pop(0))
    return search_local(shot, exclus) + melange


def choose_candidates(i, shot, cands, workdir):
    with ThreadPoolExecutor(8) as ex:
        thumbs = list(ex.map(lambda c: fetch_thumb(c, workdir), cands))
    dispo = [(c, t) for c, t in zip(cands, thumbs) if t and image_valable(t, lum_max=165, net_min=3, lum_min=15, clair_min=70)]
    if not dispo:
        return []
    dispo = dispo[:20]
    grid = make_grid([(t, f"{k + 1} {c['source'].split()[0]}") for k, (c, t) in enumerate(dispo)],
                     str(workdir / f"plan{i:02d}_choix.jpg"), cols=5)
    infos = "\n".join(f"{k + 1}. [{c['source']}] {c.get('titre', '')[:70]}" for k, (c, _) in enumerate(dispo))
    try:
        rep = ask_choix(grid, shot, dispo, infos)
    except Exception as e:
        log(f"   (tri par Claude impossible, je prends les premiers : {str(e)[:60]})")
        return [c for c, _ in dispo[:3]]
    ordre = []
    for n in rep.get("choix", []):
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(dispo) and dispo[n - 1][0] not in ordre:
            ordre.append(dispo[n - 1][0])
    return ordre or [c for c, _ in dispo[:3]]


def ask_choix(grid, shot, dispo, infos):
    return ask_json([
        img_block(grid),
        {"type": "text", "text": f"""Plan à illustrer : « {shot['description']} »
Voici {len(dispo)} clips candidats, numérotés, recadrés en vertical 9:16 comme dans la vidéo finale.
{infos}
Classe les 3 meilleurs pour une vidéo dark motivation. Critères : colle au plan, ambiance sombre et brute,
sujet bien visible dans le cadrage vertical, image nette. ÉLIMINE : texte incrusté, logo, watermark,
sous-titres, quelqu'un qui parle face caméra, ambiance joyeuse ou trop colorée, image floue/petite.
Les clips « Mes clips » viennent de la bibliothèque de l'utilisateur : privilégie-les s'ils collent.
Le clip doit montrer ce que décrit le plan, pas seulement avoir la bonne ambiance : s'il y a une phrase dite,
l'image doit l'illustrer concrètement. Sois exigeant : mieux vaut 1 seul bon clip que 3 moyens, ou aucun. Ne garde que ceux qui ont l'ambiance d'une vidéo
de dark motivation (sombre, nocturne, contrasté, effort, combat, solitude).
Réponds en JSON : {{"choix": [numéros dans l'ordre, 3 max], "raison": "courte"}}"""}], max_tokens=600,
        model=MODEL_TRI)


def pick_moment(i, shot, c, workdir):
    """Choisit l'instant de départ dans le clip et vérifie qu'il n'y a ni logo ni texte.
    Renvoie le début (s) ou None si le clip est inutilisable."""
    besoin = shot["duree"] / (RALENTI if shot.get("ralenti") else 1) + 0.15
    src, dur = resolve_apercu(c)
    if not src or not dur or dur < besoin:
        return None
    marge = dur - besoin
    n = 6 if dur <= 30 else 8
    debut = min(dur * 0.05, marge) if dur <= 30 else min(3.0, marge)
    ts = [debut + (marge - debut) * k / max(1, n - 1) for k in range(n)]
    dossier = workdir / f"plan{i:02d}_moments" / hashlib.md5(c["id"].encode()).hexdigest()[:8]
    dossier.mkdir(parents=True, exist_ok=True)

    def grab(k):
        out = str(dossier / f"{k:02d}.jpg")
        return out if grab_frame(src, ts[k] + min(1.0, besoin / 2), out, w=300, h=533) else None

    with ThreadPoolExecutor(n) as ex:
        frames = list(ex.map(grab, range(n)))
    if sum(1 for f in frames if f) < n // 2 and c["source"] == "YouTube CC" and str(src).startswith("http"):
        # le flux direct ne passe pas : on télécharge la vidéo une fois pour toutes
        c["stream"] = c["apercu"] = youtube_download(c["page"], c["id"].split(":", 1)[1])
        if not c["stream"]:
            return None
        src = c["stream"]
        with ThreadPoolExecutor(n) as ex:
            frames = list(ex.map(grab, range(n)))
    # on écarte d'office les images trop claires ou floues
    ok = [(k, f) for k, f in enumerate(frames) if f and image_valable(f)]
    if not ok:
        return None
    grid = make_grid([(f, f"{k + 1}") for k, f in ok], str(dossier / "planche.jpg"), cols=min(4, len(ok)),
                     cw=300, ch=533)
    try:
        rep = ask_moment(grid, ok, c, shot)
    except Exception as e:
        log(f"   [plan {i}] (vérification impossible, clip écarté par prudence : {str(e)[:50]})")
        return None
    if not rep.get("ok"):
        return None
    try:
        num = int(rep.get("image"))
    except (TypeError, ValueError):
        return None
    idx = {k + 1: k for k, _ in ok}.get(num)
    if idx is None:
        return None
    # pour les plans découpés en plusieurs cuts : d'autres bons moments du même clip
    autres = []
    for x in rep.get("autres") or []:
        try:
            j = {k + 1: k for k, _ in ok}.get(int(x))
        except (TypeError, ValueError):
            continue
        if j is not None and j != idx:
            autres.append(round(min(marge, ts[j]), 2))
    c["lum"] = img_stats(dict(ok)[idx])[0]
    shot["_autres"] = autres
    return round(min(marge, ts[idx]), 2)


def ask_moment(grid, ok, c, shot):
    tolerance = ("\nCe clip vient de la banque PERSONNELLE de l'utilisateur (films, combats) : un PETIT logo ou tableau "
                 "de score dans un coin ou en bas est accepté, car il disparaît au recadrage vertical. Refuse seulement "
                 "un logo/texte GROS ou au CENTRE de l'image.\n") if c.get("source") == "Mes clips" else ""
    return ask_json([
        img_block(grid),
        {"type": "text", "text": f"""Ces {len(ok)} images numérotées sont tirées d'un même clip ({c['source']}), à des
moments différents, recadrées en vertical comme dans la vidéo finale.
Je dois en extraire {shot['duree']:.1f} s pour illustrer : « {shot['description']} ».
{tolerance}
1. Inspecte CHAQUE image en entier, y compris les 4 coins et les bords : y a-t-il un logo, un watermark
   (même semi-transparent ou discret), un nom de chaîne, du texte, des sous-titres, un générique, une mire ?
   Si le même logo apparaît sur plusieurs images, il est sûrement sur tout le clip -> ok=false.
2. Parmi les images propres, choisis celle qui donne le meilleur moment pour ce plan (action nette,
   ambiance sombre MAIS sujet bien visible). Refuse les images quasi noires où on ne distingue rien.
   Refuse aussi tout ce qui est hors sujet pour une vidéo de dark motivation sur le combat et la discipline
   (animaux, calèches, bureaux, gens en costume, foule qui sourit, publicité...) -> ok=false.

3. Donne aussi dans "autres" les numéros de 2 à 4 autres images propres et utilisables (pour des cuts rapides).

JSON : {{"ok": true, "image": numéro, "autres": [numéros], "logo_detecte": false, "raison": "courte"}}"""}], max_tokens=800,
        model=MODEL_TRI)


def nouvelles_recherches(shot, deja, niveau):
    """Quand rien ne convient, Claude propose d'autres pistes de recherche pour la même phrase."""
    phrase = shot.get("phrase") or shot["description"]
    consigne = ("une autre façon de montrer EXACTEMENT ce qui est dit (synonymes, autre cadrage, autre décor)"
                if niveau == 1 else
                "une image plus simple et plus courante dans les banques vidéo, qui garde le même sens ou la même "
                "émotion (métaphore visuelle : nature, ville la nuit, entraînement, silhouette)")
    try:
        rep = ask_json(f"""Pour une vidéo dark motivation, je cherche un plan pour la phrase : « {phrase} »
(idée de départ : {shot['description'].split(' (phrase dite')[0]}).
Ces recherches n'ont rien donné d'utilisable : {', '.join(deja)}.
Propose 3 NOUVELLES requêtes en anglais (2-4 mots, concrètes), différentes des précédentes : {consigne}.
JSON : {{"recherche": ["...", "...", "..."], "description": "ce qu'on verra"}}""", max_tokens=1500)
        q = [x for x in rep.get("recherche", []) if x and x not in deja][:3]
        if q:
            return q, rep.get("description")
    except Exception:
        pass
    return None, None


def source_one(i, total, shot, workdir, exclus, pris):
    log(f"🎬 [plan {i}/{total}] {shot['description'].split(' (phrase dite')[0][:70]}")
    essayees = list(shot["recherche"])
    for tentative in range(4):
        if tentative in (1, 2):  # Claude propose d'autres pistes pour la même phrase
            q, desc = nouvelles_recherches(shot, essayees, tentative)
            if not q:
                continue
            log(f"   🔁 [plan {i}] nouvelle recherche : {', '.join(q)}"[:110])
            essayees += q
            shot = dict(shot, recherche=q)
            if desc:
                shot["description"] = desc + (f" (phrase dite : « {shot['phrase']} »)" if shot.get("phrase") else "")
        elif tentative == 3:  # dernier recours : plans d'ambiance génériques
            shot = dict(shot, recherche=["man silhouette night stock footage", "dark gym training b-roll",
                                         "storm clouds cinematic"])
        with _LOCK:
            deja = set(exclus) | set(pris)
        cands = gather_candidates(shot, deja)
        if not cands:
            continue
        for c in choose_candidates(i, shot, cands, workdir):
            with _LOCK:
                if c["id"] in pris:
                    continue
            try:
                debut = pick_moment(i, shot, c, workdir)
            except Exception as e:
                log(f"   [plan {i}] ({c['source']} illisible : {str(e)[:60]})")
                debut = None
            if debut is None:
                with _LOCK:
                    exclus.add(c["id"])
                continue
            with _LOCK:
                if c["id"] in pris:  # un autre plan l'a pris entre-temps
                    continue
                pris.add(c["id"])
            autres = shot.get("_autres", [])
            log(f"   ✅ [plan {i}] {c['source']} — {c.get('titre') or c.get('auteur') or c['id']}"[:110])
            return dict(cand=c, debut=debut, autres=autres)
    log(f"   ⚠️  [plan {i}] rien trouvé après 4 recherches : le plan précédent changera d'angle à ce moment-là.")
    return None


def source_shots(plan, workdir, hist):
    exclus = set(hist.get("utilises", {}))
    pris = set()
    plans = plan["plans"]
    log(f"\n🔎 Recherche des clips ({len(plans)} plans, {PLANS_EN_PARALLELE} en parallèle)…\n")
    with ThreadPoolExecutor(PLANS_EN_PARALLELE) as ex:
        futs = [ex.submit(source_one, i, len(plans), shot, workdir, exclus, pris)
                for i, shot in enumerate(plans, 1)]
        return [f.result() for f in futs]


# --------------------------------------------------------------------------
# 5. Montage
# --------------------------------------------------------------------------
def shot_filter(look, shot, duree, premier=False, crop=None, lum=None, glitch=False):
    L = LOOKS[look]
    f = []
    if crop:  # enlève les bandes noires d'origine
        f.append(f"crop={crop}")
    if shot.get("ralenti"):
        f.append(f"setpts={RALENTI}*PTS")
    f += [f"fps={FPS}", f"scale={W}:{H}:force_original_aspect_ratio=increase", f"crop={W}:{H}"]
    # zoom : impact au début du plan + poussée lente sur toute la durée
    Z, D, P = L["impact"], L["impact_dur"], L["pousse"]
    if Z or P:
        z = f"(1+{Z}*max(0\\,1-t/{D})+{P}*t/{max(duree, 0.1):.2f})"
        f.append(f"scale=w='2*trunc({W}*{z}/2)':h='2*trunc({H}*{z}/2)':eval=frame")
        f.append(f"crop={W}:{H}")
    couleur = shot.get("couleur") or (look == "soir" and shot.get("acte") == 2)
    # image déjà très sombre : on écrase moins les noirs pour garder le sujet visible
    noir = L["noir"] * (0.35 if lum is not None and lum < 45 else 0.7 if lum is not None and lum < 70 else 1)
    b = 0.25 - 0.13 * noir
    f.append(f"curves=master='0/0 0.25/{b:.3f} 0.6/{0.6 - 0.15 * noir:.3f} 1/0.92'")
    flash = L["flash"] if not premier else 0
    br = f"{flash}*max(0\\,1-t/0.12)" if flash else "0"
    if couleur:
        f.append(f"eq=contrast={L['contrast']}:saturation=0.45:brightness='{br}':eval=frame")
        f.append("colorbalance=rs=-0.12:gs=-0.02:bs=0.22:rm=-0.08:bm=0.15")
    else:
        f.append(f"eq=contrast={L['contrast']}:saturation=0:brightness='{br}':eval=frame")
    if L["ca"]:
        f.append(f"rgbashift=rh=-{L['ca']}:bh={L['ca']}")
    if glitch:  # lignes noires façon écran qui décroche, sur les premières images du plan
        if glitch in (True, "h", "hv"):
            f.append("drawgrid=w=iw:h=14:t=6:c=black@0.85:enable='lt(t\\,0.13)'")
        if glitch in ("v", "hv"):
            f.append("drawgrid=w=16:h=ih:t=7:c=black@0.85:enable='lt(t\\,0.13)'")
    f.append(f"noise=alls={L['grain']}:allf=t")
    f.append(f"vignette=PI/{L['vignette']}")
    # si le clip source se termine trop tôt, on fige la dernière image : le plan garde sa durée
    # exacte et la suite de la vidéo reste calée sur la voix off
    f.append(f"tpad=stop_mode=clone:stop_duration={duree + 1:.2f}")
    f.append("format=yuv420p")
    return ",".join(f)


def detect_crop(src, debut):
    """Repère les bandes noires incrustées dans la vidéo d'origine (archives, films 4:3...)."""
    try:
        r = subprocess.run([FFMPEG, "-ss", f"{debut + 0.2:.2f}", "-i", src, "-t", "1.5", "-vf",
                            "cropdetect=24:2:0", "-f", "null", "-"], capture_output=True, text=True,
                           timeout=120, encoding="utf-8", errors="replace")
        crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
        w0 = re.search(r", (\d{3,5})x(\d{3,5})", r.stderr)
        if not crops or not w0:
            return None
        w, h, x, y = (int(v) for v in crops[-1])
        W0, H0 = int(w0.group(1)), int(w0.group(2))
        if w * h < 0.9 * W0 * H0 and w > 100 and h > 100:
            return f"{w}:{h}:{x}:{y}"
    except Exception:
        pass
    return None


def lum_video(path):
    """Luminosité moyenne (0-255, échelle vidéo 16-235) d'un plan rendu."""
    try:
        r = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-vf",
                            "fps=4,scale=90:160,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        v = [float(x) for x in re.findall(r"YAVG=([0-9.]+)", r.stdout)]
        return sum(v) / len(v) if v else 99
    except Exception:
        return 99


def render_shot(src, debut, shot, look, out, premier=False, crop=None, lum=None, glitch=False, duree=None):
    duree = duree or shot["duree"]
    run([FFMPEG, "-y", "-v", "error", "-ss", f"{debut:.3f}", "-i", src, "-an",
         "-vf", shot_filter(look, shot, duree, premier, crop, lum, glitch), "-t", f"{duree:.3f}",
         "-r", FPS, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", out], timeout=600)
    # garde-fou : si le plan sort presque noir, on le refait sans écraser les noirs
    if lum_video(out) < 24:
        run([FFMPEG, "-y", "-v", "error", "-ss", f"{debut:.3f}", "-i", src, "-an",
             "-vf", shot_filter(look, shot, duree, premier, crop, 0, glitch) + ",eq=brightness=0.06:gamma=1.25",
             "-t", f"{duree:.3f}", "-r", FPS, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", out],
            timeout=600)
    return out


def assemble(plans, clips, look, workdir, sync=False):
    seg = workdir / "plans"
    seg.mkdir(exist_ok=True)
    retenus = [[dict(p), c] for p, c in zip(plans, clips) if c]
    if not retenus:
        stop("Aucun clip utilisable trouvé. Essaie un autre thème ou vérifie tes clés API.")
    manque = sum(p["duree"] for p, c in zip(plans, clips) if not c)
    if manque and sync:
        # mode script : le plan précédent dure plus longtemps, pour rester calé sur la voix
        retenus, avant = [], 0.0
        for p, c in zip(plans, clips):
            if c:
                retenus.append([dict(p, duree=p["duree"] + avant), c] if not retenus else [dict(p), c])
                avant = 0.0
            elif retenus:
                # le plan précédent s'allonge, mais on change d'angle (autre moment du même clip) au bon moment
                retenus[-1][0]["duree"] += p["duree"]
                retenus[-1][0]["coupes"] = retenus[-1][0].get("coupes", 1) + 1
            else:
                avant += p["duree"]
        manque = 0
    if sync and retenus:  # marge pour que la vidéo ne soit jamais plus courte que la voix off (arrondis d'images)
        retenus[-1][0]["duree"] += 0.25
    # sinon, le temps des plans introuvables est réparti sur les autres (pas un plan géant)
    if manque:
        tot = sum(p["duree"] for p, _ in retenus)
        for p, _ in retenus:
            p["duree"] = round(p["duree"] * (tot + manque) / tot, 3)

    log("\n✂️  Découpe et étalonnage des plans…")
    L = LOOKS[look]
    fichiers = []
    rnd = random.Random(1)
    for k, (p, c) in enumerate(retenus):
        cand = c["cand"]
        src, _ = resolve(cand)
        moments = [c["debut"]] + [t for t in c.get("autres", []) if abs(t - c["debut"]) > 0.5]
        n = min(p.get("coupes", 1), len(moments)) if p.get("coupes", 1) > 1 else 1
        crop = detect_crop(src, c["debut"])
        for j in range(n):
            d = p["duree"] / n
            debut = moments[j]
            besoin = d / (RALENTI if p.get("ralenti") else 1)
            if cand.get("duree") and debut + besoin > cand["duree"]:
                debut = max(0, cand["duree"] - besoin - 0.05)
            out = seg / f"{k:02d}_{j}.mp4"
            # glitch : sur une partie des cuts, et toujours à la bascule de l'acte 2 du soir
            bascule = look == "soir" and p.get("acte") == 2 and (k == 0 or retenus[k - 1][0].get("acte") != 2) and j == 0
            glitch = bascule or (k + j > 0 and rnd.random() < L["glitch"])
            if glitch:
                glitch = "hv" if bascule else rnd.choice(["h", "h", "v", "hv"])
            args = dict(premier=(k == 0 and j == 0), crop=crop, lum=cand.get("lum"), glitch=glitch, duree=d)
            try:
                render_shot(src, debut, p, look, str(out), **args)
            except Exception:
                if not cand.get("apercu"):
                    raise
                render_shot(cand["apercu"], debut, p, look, str(out), **args)
            fichiers.append(out)
        log(f"   plan {k + 1}/{len(retenus)} ok" + (f" ({n} cuts)" if n > 1 else ""))
    (workdir / "liste.txt").write_text("".join(f"file 'plans/{f.name}'\n" for f in fichiers), encoding="utf-8")
    run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "liste.txt", "-c", "copy",
         "video.mp4"], cwd=workdir)
    total = probe_duration(str(workdir / "video.mp4")) or sum(p["duree"] for p, _ in retenus)
    fondu = f"fade=t=in:st=0:d={L['fondu']}," if L.get("fondu") else ""
    run([FFMPEG, "-y", "-v", "error", "-i", "video.mp4",
         "-vf", f"{fondu}fade=t=out:st={max(0, total - 0.5):.2f}:d=0.5",
         "-an", "-r", FPS, "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-maxrate", "9M",
         "-bufsize", "18M", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "final.mp4"],
        cwd=workdir, timeout=1800)
    return workdir / "final.mp4", retenus


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
def afficher_plan(plan):
    log(f"\n📝 « {plan.get('titre', '')} »")
    t = 0.0
    for i, p in enumerate(plan["plans"], 1):
        extras = ((" [ralenti]" if p.get("ralenti") else "") + (" [couleur]" if p.get("couleur") else "")
                  + (f" [acte {p['acte']}]" if p.get("acte") else "") + (f" [{p['coupes']} cuts]" if p.get("coupes", 1) > 1 else ""))
        desc = p["description"].split(" (phrase dite")[0]
        log(f"  {i:>2}. {t:5.1f}s +{p['duree']:.1f}s  {desc[:60]}{extras}")
        if p.get("phrase") and not p.get("suite_phrase"):
            log(f"       🎙️  « {p['phrase'][:80]} »")
            if p.get("traduction"):
                log(f"       🇫🇷  {p['traduction'][:80]}")
        t += p["duree"]
    log(f"  Durée totale : {t:.1f} s")


def verifier():
    if not FFMPEG or not FFPROBE:
        stop("ffmpeg n'est pas installé ou introuvable (voir LISEZMOI.md, étape 1).")
    for d in (D_CLIPS, D_SORTIES, D_CACHE, D_SCRIPTS):
        d.mkdir(parents=True, exist_ok=True)
    manquantes = [k for k in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "YOUTUBE_API_KEY") if not os.getenv(k, "").strip()]
    if manquantes:
        log("ℹ️  Clés absentes (ces sources seront ignorées) : " + ", ".join(manquantes))


def main():
    ap = argparse.ArgumentParser(description="Studio dark motivation (montage visuel)")
    ap.add_argument("--format", choices=list(FORMATS), help="long (50 s - 1 min 10) ou court (6-8 s)")
    ap.add_argument("--theme")
    ap.add_argument("--script", help="fichier du dossier scripts (.txt une phrase par ligne + .mp3 du même nom, ou .srt)")
    ap.add_argument("--oui", action="store_true", help="ne pas demander de validation")
    args = ap.parse_args()

    verifier()
    log("\n🖤 STUDIO DARK MOTIVATION — montage visuel\n")

    fmt = args.format
    if not fmt:
        log("Quel format ?\n  1. Long — storytelling (50 s à 1 min 10)\n  2. Court — punch (6 à 8 s)")
        fmt = {"1": "long", "2": "court"}.get(input("> ").strip()[:1], "long")

    nom_script = args.script
    if nom_script is None:
        dispo = sorted(p.name for p in D_SCRIPTS.glob("*") if p.suffix.lower() in (".txt", ".srt"))
        if not dispo:
            stop("Aucun script trouvé. Mets dans le dossier « scripts » ton texte (une phrase par ligne, en .txt) "
                 "et ta voix off avec le même nom (ex : revanche.txt + revanche.mp3).")
        log("\nQuel script ?")
        for k, n in enumerate(dispo, 1):
            audio = any((D_SCRIPTS / n).with_suffix(e).exists() for e in AUDIO_EXT)
            log(f"  {k}. {n}" + ("   🎙️ voix off trouvée" if audio or n.lower().endswith('.srt') else "   ⚠️ pas de voix off (timing estimé)"))
        r = input("> ").strip()
        nom_script = dispo[int(r) - 1] if r.isdigit() and 1 <= int(r) <= len(dispo) else dispo[0]
    chemin = Path(nom_script) if Path(nom_script).exists() else D_SCRIPTS / nom_script
    if not chemin.exists():
        stop(f"Script introuvable : {chemin}")
    script = lire_script(chemin)
    if not script:
        stop("Le script est vide ou illisible.")
    if chemin.suffix.lower() == ".srt":
        mode = "timings exacts (.srt)"
    elif script[0].get("audio"):
        mode = f"calé sur ta voix off {Path(script[0]['audio']).name}"
    else:
        mode = "timing estimé d'après le nombre de mots (ajoute un .mp3 du même nom pour un calage exact)"
    log(f"   📜 {len(script)} phrases, {script[-1]['fin']:.1f} s — {mode}")
    a, b = FORMATS[fmt]["duree"]
    if not (a - 3 <= script[-1]["fin"] <= b + 5):
        log(f"   ℹ️  ta voix off dure {script[-1]['fin']:.0f} s (format prévu : {a}-{b} s), je m'adapte.")

    theme = args.theme if args.theme is not None else input(
        "\nMots-clés / ambiance en plus du script (ex : boxe, nuit, pluie — Entrée = aucun) :\n> ").strip()
    theme = theme or "dark motivation, sport de combat, discipline"

    look = FORMATS[fmt]["look"]
    while True:
        log("\n🧠 Claude prépare la liste des plans…")
        plan = plan_depuis_script(theme, fmt, script)
        afficher_plan(plan)
        if args.oui:
            break
        rep = input("\nOn lance ? (o = oui / r = refaire la liste / n = annuler) > ").strip().lower()
        if rep.startswith("o") or rep == "":
            break
        if rep.startswith("n"):
            return

    nom = f"{dt.datetime.now():%Y-%m-%d_%Hh%M}_{fmt}_{slug(plan.get('titre') or theme)}"
    workdir = D_CACHE / nom
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    global T0
    T0 = time.time()
    hist = load_hist()
    clips = source_shots(plan, workdir, hist)
    final, retenus = assemble(plan["plans"], clips, look, workdir, sync=plan.get("sync", False))

    sortie = D_SORTIES / f"{nom}.mp4"
    shutil.copy(final, sortie)
    lignes, credits_cc = ["SOURCES DES PLANS :"], []
    for k, (p, c) in enumerate(retenus, 1):
        cd = c["cand"]
        lignes.append(f"{k:>2}. {cd['source']} | {cd.get('auteur', '')} | {cd.get('page', '')} | {cd.get('licence', '')}")
        if cd["source"] in ("YouTube CC", "Internet Archive") and cd.get("auteur"):
            credits_cc.append(cd["auteur"])
        hist["utilises"][cd["id"]] = {"date": f"{dt.date.today()}", "video": nom}
    if credits_cc:
        lignes += ["", "À COLLER DANS TA DESCRIPTION TIKTOK (crédit obligatoire Creative Commons) :",
                   "Images : " + ", ".join(dict.fromkeys(credits_cc)) + " (CC BY)"]
    if plan.get("sync"):
        lignes += ["", "CALAGE SUR TON SCRIPT (moment -> phrase) :"]
        t = 0.0
        for p, _ in retenus:
            if p.get("phrase"):
                lignes.append(f"{t:6.1f}s  « {p['phrase']} »"
                              + (f"\n         ({p['traduction']})" if p.get("traduction") else ""))
            t += p["duree"]
    (D_SORTIES / f"{nom}_credits.txt").write_text("\n".join(lignes), encoding="utf-8")
    save_hist(hist)

    # ménage : on ne garde que le plan (plan.json) des fichiers de travail, les vidéos intermédiaires
    # pèsent plusieurs centaines de Mo par montage
    if os.getenv("GARDER_CACHE", "").strip() != "1":
        for p in list(workdir.iterdir()):
            if p.name != "plan.json":
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        for p in D_CACHE.glob("yt_*"):
            p.unlink(missing_ok=True)

    log(f"\n✅ Vidéo prête (sans son, sans texte) : {sortie}")
    log(f"   Crédits : {D_SORTIES / (nom + '_credits.txt')}")
    log(f"   Claude : {USAGE['appels']} appels, {USAGE['in']:,} jetons envoyés, {USAGE['out']:,} reçus")
    log(f"   Durée totale : {time.time() - T0:.0f} s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nAnnulé.")
