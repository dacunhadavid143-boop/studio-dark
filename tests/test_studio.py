"""Tests sans clé API ni réseau : calage sur la voix off, découpe en plans, montage ffmpeg."""
import shutil
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio as S  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg requis")


def fausse_voix(path, lignes, seed=3):
    """Voix synthétique au débit irrégulier, avec des pauses dramatiques AU MILIEU de certaines phrases.
    Renvoie le vrai moment de début de chaque ligne."""
    sr, rng = 16000, np.random.default_rng(seed)
    audio, t, vrais = [np.zeros(int(0.6 * sr))], 0.6, []
    for k, ligne in enumerate(lignes):
        vrais.append(t)
        mots = ligne.split()
        for i, _ in enumerate(mots):
            d = rng.uniform(0.25, 0.7)
            n = int(d * sr)
            audio.append(0.3 * np.sin(2 * np.pi * 180 * np.arange(n) / sr) * rng.uniform(0.5, 1, n))
            t += d
            p = rng.uniform(0.5, 0.8) if (i == len(mots) // 2 and k % 2 == 0) else 0.05
            audio.append(np.zeros(int(p * sr)))
            t += p
        p = rng.uniform(0.7, 1.3)
        audio.append(np.zeros(int(p * sr)))
        t += p
    a = (np.concatenate(audio) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(a.tobytes())
    return vrais


LIGNES = ["A young man walks alone in the cold.", "No one believes in him.",
          "Every morning he rises before everyone else.", "He hits the bag until his hands go numb.",
          "Today, they watch him."]


def test_calage_sur_la_voix(tmp_path):
    vrais = fausse_voix(tmp_path / "test.wav", LIGNES)
    (tmp_path / "test.txt").write_text("\n".join(LIGNES), encoding="utf-8")
    segs = S.lire_script(tmp_path / "test.txt")
    assert [s["texte"] for s in segs] == LIGNES
    for s, v in zip(segs, vrais):
        assert abs(s["debut"] - v) < 0.15, (s, v)  # au dixième de seconde près


def test_regroupement_respecte_la_duree(tmp_path):
    fausse_voix(tmp_path / "t.wav", LIGNES)
    (tmp_path / "t.txt").write_text("\n".join(LIGNES), encoding="utf-8")
    segs = S.lire_script(tmp_path / "t.txt")
    plans = S.regrouper_segments(segs, 2.0, 5.0)
    total = sum(p["duree"] for p in plans)
    assert abs(total - segs[-1]["fin"]) < 0.05  # aucun trou, aucune dérive
    assert all(p["duree"] <= 5.0 + 1e-6 for p in plans)


def test_script_sans_voix_estime_les_timings(tmp_path):
    (tmp_path / "s.txt").write_text("# commentaire ignoré\nPremière phrase courte.\nDeuxième phrase.", encoding="utf-8")
    segs = S.lire_script(tmp_path / "s.txt")
    assert len(segs) == 2 and segs[1]["debut"] > segs[0]["fin"]


def test_plan_trop_court_garde_sa_duree(tmp_path):
    """Un clip source plus court que le plan est prolongé (dernière image figée) : pas de décalage."""
    src = tmp_path / "court.mp4"
    S.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=1", str(src)])
    out = tmp_path / "plan.mp4"
    S.render_shot(str(src), 0.2, {"duree": 3.0}, "hebdo", str(out), duree=3.0)
    assert abs(S.probe_duration(str(out)) - 3.0) < 0.05


def test_rendu_vertical(tmp_path):
    src = tmp_path / "paysage.mp4"
    S.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=25:d=2", str(src)])
    out = tmp_path / "v.mp4"
    S.render_shot(str(src), 0, {"duree": 1.5}, "soir", str(out), duree=1.5)
    r = S.run([S.FFPROBE, "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)])
    assert r.strip() == f"{S.W},{S.H}"
