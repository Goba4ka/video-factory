from pathlib import Path

import numpy as np

import analyze_refs as a


base = Path("candidates/licensed")
for name in ["scott-buckley-nightfall.mp3", "scott-buckley-resonance.mp3"]:
    stereo, sr = a.decode_f32(base / name)
    mono = stereo.mean(axis=1).astype(float)
    print(f"\n{name}")
    for start in range(0, int(len(mono) / sr) - 28, 15):
        window = mono[int(start * sr) : int((start + 28.37) * sr)]
        metrics = a.tempo_and_spectrum(window, sr)
        rms = a.db(np.sqrt(np.mean(window * window) + 1e-12))
        print(
            f"{start:3d}-{start + 28.37:6.2f} "
            f"rms={rms:6.2f} bpm={metrics['tempo_bpm_est']:6.2f} "
            f"rel={metrics['tempo_reliability']:5.2f} "
            f"low={metrics['low_band_energy_ratio']:.3f} "
            f"centroid={metrics['spectral_centroid_hz']:.0f}"
        )
