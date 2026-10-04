"""#206 · diagnóstico (no arregla nada, siempre pasa): reproduce los pasos de
detect_tempo a 136 BPM sin etiqueta y deja en el log del CI la semilla de librosa, las
candidatas y sus puntajes, para decidir el arreglo con datos."""
import os
import sys
import warnings

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_detect as gd  # noqa: E402
from test_206_proporcion_media_rejilla import SR_ARCHIVO, con_contratiempo  # noqa: E402


def test_diagnostico_136(tmp_path):
    import librosa
    import soundfile as sf

    ruta = tmp_path / "d136.wav"
    sf.write(ruta, con_contratiempo(136), SR_ARCHIVO)
    y, sr = librosa.load(str(ruta), sr=11025, mono=True)
    env = gd._onset_env(y, sr)
    env3 = gd._max3(env)
    dur = len(y) / sr
    t, _ = librosa.beat.beat_track(y=y, sr=sr, trim=False)
    cruda = float(np.atleast_1d(t)[0])
    octavas, otras = gd.semillas_candidatas(cruda)
    lineas = [f"cruda={cruda:.3f} octavas={[round(o, 2) for o in octavas]} otras={[round(o, 2) for o in otras]}"]
    for s in octavas + otras:
        bpm, sc = gd._busqueda_gruesa(env, sr, dur, s, env3)
        lineas.append(f"semilla={s:.2f} → gruesa bpm={bpm:.2f} puntaje={sc:.4f}")
    for bpm in (68.0, 90.67, 136.0, 272.0 / 2):
        p = 60.0 / bpm
        sc = max(gd._grid_score(env, sr, p, ph, dur, env3) for ph in np.arange(0, p, p / 8))
        lineas.append(f"rejilla fija {bpm:.2f}: puntaje={sc:.4f}")
    final, _ = gd.detect_tempo(y, sr, seed_bpm=None)
    lineas.append(f"detect_tempo={final}")
    warnings.warn("#206 DIAG " + " | ".join(lineas))
