"""Clonage de voix XTTS-v2 (Coqui, fork idiap) — usage NON commercial (licence CPML).
Usage :
  GENERER_VOIX.bat                    -> lit texte.txt, voix de reference.wav, écrit sortie.wav
  GENERER_VOIX.bat "Ton texte" en     -> texte et langue en arguments (en, fr, es, de...)
Les erreurs sont enregistrées dans generation_log.txt.
"""
import os
import sys
import time
import traceback
from pathlib import Path

ICI = Path(__file__).resolve().parent
LOG = ICI / "generation_log.txt"


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def main():
    LOG.write_text(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n", encoding="utf-8")
    os.environ["COQUI_TOS_AGREED"] = "1"  # accepte la licence CPML (non commerciale)

    import numpy as np
    import soundfile as sf
    import torch
    import torchaudio

    # Lecture audio sans FFmpeg/torchcodec (évite l'erreur de DLL FFmpeg sous Windows)
    def _load(path, *a, **k):
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        return torch.from_numpy(np.ascontiguousarray(data.T)), sr

    torchaudio.load = _load

    from TTS.api import TTS

    ref = [p for p in (ICI / "reference.wav", ICI / "reference.mp3") if p.exists()]
    if not ref:
        log("❌ Mets un fichier reference.wav (10 à 30 s de voix propre, sans musique) dans ce dossier.")
        return
    if len(sys.argv) > 1:
        texte = sys.argv[1]
    else:
        f = ICI / "texte.txt"
        if not f.exists():
            log("❌ Écris ton texte dans texte.txt (ou passe-le en argument).")
            return
        texte = f.read_text(encoding="utf-8-sig").strip()
    langue = sys.argv[2] if len(sys.argv) > 2 else "en"

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"Appareil : {device}" + (f" ({torch.cuda.get_device_name(0)})" if device == "cuda" else " — lent, GPU non détecté"))
    log("Chargement du modèle (le premier lancement télécharge ~2 Go)…")
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)

    sortie = ICI / "sortie.wav"
    log(f"Voix de référence : {ref[0].name} — langue : {langue}")
    log(f"Texte : {texte[:120]}")
    t0 = time.time()
    tts.tts_to_file(text=texte, speaker_wav=str(ref[0]), language=langue, file_path=str(sortie))
    log(f"✅ Fichier généré : {sortie} ({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("❌ ERREUR :\n" + traceback.format_exc())
