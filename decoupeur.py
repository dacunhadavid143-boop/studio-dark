#!/usr/bin/env python3
"""
DÉCOUPEUR DE FILMS — transforme des films / combats entiers en banque de scènes pour le studio.

1. Mets tes vidéos entières dans le dossier « a_decouper ».
2. Lance DECOUPER_FILMS.bat (ou : python decoupeur.py).
3. Chaque vidéo est découpée scène par scène (détection des changements de plan),
   Claude regarde chaque scène, jette les inutilisables (texte, générique, noir, logo, gens qui parlent)
   et donne aux autres un nom en anglais (ex : roman_legion_march, ko_punch_ring, ships_burning).
4. Les scènes arrivent dans mes_clips/<nom de la vidéo>/ — le studio les utilise automatiquement.

Une vidéo déjà découpée n'est pas refaite (liste dans a_decouper/deja_decoupes.json).
"""
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import studio as S  # réutilise Claude, ffmpeg et les planches d'images du studio

D_ENTREE = S.DATA / "a_decouper"
FAIT = D_ENTREE / "deja_decoupes.json"

SEUIL_SCENE = 0.32       # sensibilité de détection des changements de plan
SCENE_MIN = 1.2          # une scène plus courte est ignorée (flash, transition)
SCENE_MAX = 12.0         # une scène plus longue est coupée en morceaux de cette taille
PAR_PLANCHE = 20         # nombre de scènes montrées à Claude par image
log = S.log


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s[:60] or "scene"


def analyser(film, tmp):
    """Un seul passage sur la vidéo : détection des coupes + 1 vignette par seconde."""
    vign = tmp / "v"
    vign.mkdir(parents=True, exist_ok=True)
    cmd = [S.FFMPEG, "-hide_banner", "-i", str(film), "-filter_complex",
           f"[0:v]scale=320:-2,split[a][b];[a]select='gt(scene,{SEUIL_SCENE})',showinfo[c];[b]fps=1[d]",
           "-map", "[c]", "-f", "null", "-", "-map", "[d]", "-q:v", "5", str(vign / "%06d.jpg")]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    coupes = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", p.stderr)]
    return coupes, vign


def scenes_depuis(coupes, duree):
    bornes = [0.0] + [c for c in coupes if 0.2 < c < duree - 0.2] + [duree]
    scenes = []
    for a, b in zip(bornes, bornes[1:]):
        a, b = a + 0.15, b - 0.15  # on évite les images de transition
        if b - a < SCENE_MIN:
            continue
        n = max(1, int((b - a) // SCENE_MAX) + (1 if (b - a) % SCENE_MAX > SCENE_MIN else 0))
        pas = (b - a) / n
        for k in range(n):
            scenes.append((round(a + k * pas, 2), round(min(b, a + (k + 1) * pas), 2)))
    return scenes


def vignette(vign, t):
    f = vign / f"{max(1, int(t) + 1):06d}.jpg"
    return f if f.exists() else None


def trier(lot, film_nom, k0, tmp):
    """lot = [(scene, vignette)] -> [(scene, nom)] gardées."""
    grille = S.make_grid([(str(v), str(k0 + i + 1)) for i, (_, v) in enumerate(lot)],
                         str(tmp / f"planche_{k0:05d}.jpg"), cols=5, cw=288, ch=162)
    try:
        rep = S.ask_json([
            S.img_block(grille),
            {"type": "text", "text": f"""Ces {len(lot)} images numérotées ({k0 + 1} à {k0 + len(lot)}) sont des scènes de la vidéo
« {film_nom} ». Je construis une banque de plans pour des montages TikTok de dark motivation
(combat, entraînement, guerre, histoire, solitude, puissance), recadrés plus tard en vertical au centre.

Pour CHAQUE image, décide si on la garde :
- JETER : carton de texte / sous-titres incrustés / générique / logo de chaîne / écran noir ou fondu /
  image floue ou illisible / gros plan de quelqu'un qui parle face caméra sans action / scène banale sans intérêt.
- GARDER : action, combat, coup, foule, armée, feu, navires, nature puissante, silhouette, regard intense,
  victoire, effort, décor impressionnant.
Pour les scènes gardées, donne un nom EN ANGLAIS de 3 à 6 mots, concret et visuel, qui décrit ce qu'on voit
(ex : "roman legion marching", "ship burning at sea", "boxer knockout punch", "crowd cheering arena",
"man walking alone in fog"). Ce nom sert de mots-clés de recherche : sois précis.

JSON : {{"scenes": [{{"n": numéro, "garder": true, "nom": "..."}}]}}"""}], max_tokens=4000)
    except Exception as e:
        log(f"   (tri impossible pour ce lot : {str(e)[:60]})")
        return []
    out = []
    for s in rep.get("scenes", []):
        try:
            i = int(s.get("n")) - k0 - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(lot) and s.get("garder") and s.get("nom"):
            out.append((lot[i][0], s["nom"]))
    return out


def couper(film, a, b, sortie):
    S.run([S.FFMPEG, "-y", "-v", "error", "-ss", f"{a:.2f}", "-i", str(film), "-t", f"{b - a:.2f}", "-an",
           "-vf", "scale=-2:'min(1080,ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(sortie)], timeout=600)


def decouper(film, fait):
    t0 = time.time()
    duree = S.probe_duration(str(film)) or 0
    if duree < 3:
        log(f"⚠️  {film.name} : vidéo illisible ou trop courte, ignorée.")
        return
    nom = slug(film.stem)
    log(f"\n🎞️  {film.name} ({duree / 60:.0f} min) — détection des scènes…")
    tmp = S.D_CACHE / f"decoupe_{nom}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    coupes, vign = analyser(film, tmp)
    scenes = [(a, b) for a, b in scenes_depuis(coupes, duree)]
    avec_vign = [((a, b), vignette(vign, (a + b) / 2)) for a, b in scenes]
    avec_vign = [(s, v) for s, v in avec_vign if v and S.image_valable(str(v), lum_max=200, net_min=3,
                                                                       lum_min=10, clair_min=50)]
    log(f"   {len(scenes)} scènes trouvées, {len(avec_vign)} lisibles — Claude fait le tri…")

    lots = [avec_vign[k:k + PAR_PLANCHE] for k in range(0, len(avec_vign), PAR_PLANCHE)]
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda kl: trier(kl[1], film.stem, kl[0] * PAR_PLANCHE, tmp), enumerate(lots)))
    garder = [x for r in res for x in r]
    log(f"   {len(garder)} scènes gardées — découpe en cours…")

    dossier = S.D_CLIPS / nom
    dossier.mkdir(parents=True, exist_ok=True)
    noms = {}

    def job(item):
        (a, b), titre = item
        base = slug(titre)
        with S._LOCK:
            noms[base] = noms.get(base, 0) + 1
            n = noms[base]
        out = dossier / (f"{base}_{n:02d}.mp4" if n > 1 else f"{base}.mp4")
        try:
            couper(film, a, b, out)
            return 1
        except Exception as e:
            log(f"   (scène {a:.0f}s ratée : {str(e)[:60]})")
            return 0

    with ThreadPoolExecutor(3) as ex:
        ok = sum(ex.map(job, garder))
    shutil.rmtree(tmp, ignore_errors=True)
    fait[film.name] = {"date": time.strftime("%Y-%m-%d"), "scenes": ok, "dossier": str(dossier.name)}
    FAIT.write_text(json.dumps(fait, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"   ✅ {ok} scènes ajoutées dans mes_clips/{dossier.name} ({time.time() - t0:.0f} s)")


def main():
    if not S.FFMPEG:
        S.stop("ffmpeg introuvable.")
    D_ENTREE.mkdir(exist_ok=True)
    S.D_CLIPS.mkdir(exist_ok=True)
    try:
        fait = json.loads(FAIT.read_text(encoding="utf-8"))
    except Exception:
        fait = {}
    films = sorted(p for p in D_ENTREE.iterdir() if p.suffix.lower() in S.VIDEO_EXT and p.name not in fait)
    if not films:
        log("Aucune nouvelle vidéo dans le dossier « a_decouper ». Mets-y tes films / combats entiers et relance.")
        if fait:
            log(f"(Déjà découpées : {', '.join(fait)})")
        return
    log(f"🖤 DÉCOUPEUR — {len(films)} vidéo(s) à découper : {', '.join(p.name for p in films)}")
    for film in films:
        try:
            decouper(film, fait)
        except Exception as e:
            log(f"❌ {film.name} : {str(e)[:200]}")
    total = sum(1 for _ in S.D_CLIPS.rglob("*.mp4"))
    log(f"\n📚 Ta banque contient maintenant {total} scènes dans mes_clips.")
    log(f"   Claude : {S.USAGE['appels']} appels, {S.USAGE['in']:,} jetons envoyés, {S.USAGE['out']:,} reçus")
    log("   Tu peux supprimer les vidéos entières du dossier a_decouper pour gagner de la place.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nAnnulé.")
