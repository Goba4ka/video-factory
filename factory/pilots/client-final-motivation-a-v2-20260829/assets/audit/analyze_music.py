import json
import sys
from pathlib import Path

import librosa
import numpy as np


def main() -> None:
    source = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    y, sr = librosa.load(source, sr=22050, mono=True)
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    tempo, beats = librosa.beat.beat_track(onset_envelope=onset, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beats, sr=sr).tolist()
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr)
    loudest = np.argsort(rms)[-20:]
    payload = {
        "source": str(source),
        "duration": round(float(librosa.get_duration(y=y, sr=sr)), 3),
        "tempo_bpm": round(float(np.asarray(tempo).reshape(-1)[0]), 3),
        "beat_times": [round(float(t), 3) for t in beat_times],
        "loudest_moments": [round(float(rms_times[i]), 3) for i in sorted(loudest.tolist())],
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "tempo_bpm": payload["tempo_bpm"], "beats": len(beat_times)}))


if __name__ == "__main__":
    main()
