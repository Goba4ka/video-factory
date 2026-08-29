import json
import math
import subprocess
from pathlib import Path

import numpy as np


FFMPEG = Path(r"C:\Users\ns277\bin\ffmpeg.exe")
DOWNLOADS = Path(r"C:\Users\ns277\Downloads")
ROOT = Path(__file__).resolve().parent

FILES = [
    "ogjb1N3eMRef0MRYAIXAIvjN1D8uTHLgQ2AFIj.mp4",
    "owGEDV4mqIA3hC0XROegufgW4QE2nBD6qQYRoF.mp4",
    "okDojoGYsNMeJBU1YfTfMLjIQ9OLIIUYGEgAjw.mp4",
    "o0DOqXEEIDQqX6hfCQFmHnumfB7VYEAxDBzRQ7.mp4",
    "oYiwFBE1RAhKBRNQi64iWIWAzKMfEmRlC2qoIB.mp4",
    "oQM4Q3BqBg0IPERE1E5jfHFAN7jDhFmnvcjeME.mp4",
    "o0I1EQLyjVjaAkMRfXCZ8uFfzpeIDQBAIhqbDd.mp4",
    "oATPD9oIS8GRzQUgIGLqAe4GAcYeANXjefDRyA.mp4",
    "ooehMGeIQAeept0QEQFVGgAKNcgsLARQGMnwC2.mp4",
    "owgeQKD3jYGHAGMTjWIr8LfrcADIdQ3AofmEC0.mp4",
]


def ffmpeg_text(args):
    proc = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-nostats", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def decode_f32(path, sr=22050):
    proc = subprocess.run(
        [
            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-vn", "-ar", str(sr), "-ac", "2", "-f", "f32le", "-acodec", "pcm_f32le", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    audio = np.frombuffer(proc.stdout, dtype="<f4")
    return audio.reshape(-1, 2), sr


def db(value, floor=-120.0):
    return float(max(floor, 20.0 * math.log10(max(float(value), 10 ** (floor / 20.0)))))


def loudnorm(path):
    _, _, err = ffmpeg_text([
        "-i", str(path), "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "NUL"
    ])
    start = err.rfind("{")
    end = err.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"loudnorm JSON missing for {path}")
    data = json.loads(err[start : end + 1])
    return {
        "lufs_i": float(data["input_i"]),
        "true_peak_dbtp": float(data["input_tp"]),
        "lra_lu": float(data["input_lra"]),
        "threshold_lufs": float(data["input_thresh"]),
    }


def frame_rms(signal, sr, window_s=0.5, hop_s=0.25):
    n = max(1, int(window_s * sr))
    hop = max(1, int(hop_s * sr))
    starts = np.arange(0, max(1, len(signal) - n + 1), hop)
    values = np.array([np.sqrt(np.mean(signal[s : s + n] ** 2) + 1e-12) for s in starts])
    times = (starts + n / 2) / sr
    return times, values


def tempo_and_spectrum(mono, sr):
    n_fft = 1024
    hop = 256
    if len(mono) < n_fft:
        mono = np.pad(mono, (0, n_fft - len(mono)))
    frames = np.lib.stride_tricks.sliding_window_view(mono, n_fft)[::hop]
    window = np.hanning(n_fft).astype(np.float32)
    mag = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float64)
    power = mag * mag + 1e-18
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)

    delta = np.maximum(0.0, mag[1:] - mag[:-1])
    flux = np.concatenate([[0.0], delta.sum(axis=1)])
    flux = np.maximum(0.0, flux - np.median(flux))
    if flux.max() > 0:
        flux /= flux.max()
    frame_rate = sr / hop
    centered = flux - flux.mean()
    min_lag = max(1, int(frame_rate * 60 / 180))
    max_lag = min(len(centered) - 2, int(frame_rate * 60 / 50))
    lags = np.arange(min_lag, max_lag + 1)
    ac = np.array([np.dot(centered[:-lag], centered[lag:]) for lag in lags])
    if len(ac) and np.any(np.isfinite(ac)):
        best_i = int(np.nanargmax(ac))
        best_lag = int(lags[best_i])
        bpm = 60.0 * frame_rate / best_lag
        positive = np.maximum(ac, 0)
        reliability = float(positive[best_i] / (np.percentile(positive, 75) + 1e-9))
    else:
        bpm, reliability = 0.0, 0.0

    threshold = np.percentile(flux, 82)
    min_peak_gap = int(0.22 * frame_rate)
    peaks = []
    last = -min_peak_gap
    for i in range(1, len(flux) - 1):
        if flux[i] >= threshold and flux[i] >= flux[i - 1] and flux[i] > flux[i + 1] and i - last >= min_peak_gap:
            peaks.append(i)
            last = i
    if len(peaks) > 2:
        iois = np.diff(peaks) / frame_rate
        median_ioi = float(np.median(iois))
        ioi_cv = float(np.std(iois) / (np.mean(iois) + 1e-9))
    else:
        median_ioi, ioi_cv = 0.0, 0.0

    mean_power = power.mean(axis=0)
    total = mean_power[(freqs >= 60) & (freqs <= 12000)].sum() + 1e-18
    vocal = mean_power[(freqs >= 300) & (freqs <= 3500)].sum() / total
    low = mean_power[(freqs >= 60) & (freqs < 250)].sum() / total
    air = mean_power[(freqs >= 6000) & (freqs <= 12000)].sum() / total
    centroid = float((mean_power * freqs).sum() / (mean_power.sum() + 1e-18))
    flatness = float(np.exp(np.mean(np.log(mean_power + 1e-18))) / (np.mean(mean_power + 1e-18)))

    vocal_env = power[:, (freqs >= 300) & (freqs <= 3500)].sum(axis=1)
    log_env = np.log(vocal_env + np.percentile(vocal_env, 10) + 1e-12)
    log_env -= log_env.mean()
    mod = np.abs(np.fft.rfft(log_env * np.hanning(len(log_env)))) ** 2
    mod_freqs = np.fft.rfftfreq(len(log_env), 1 / frame_rate)
    denom = mod[(mod_freqs >= 0.2) & (mod_freqs <= 20)].sum() + 1e-18
    speech_mod = float(mod[(mod_freqs >= 2) & (mod_freqs <= 8)].sum() / denom)

    return {
        "tempo_bpm_est": round(float(bpm), 2),
        "tempo_reliability": round(float(reliability), 3),
        "onset_median_spacing_s": round(median_ioi, 3),
        "onset_spacing_cv": round(ioi_cv, 3),
        "onset_count": len(peaks),
        "vocal_band_energy_ratio": round(float(vocal), 4),
        "low_band_energy_ratio": round(float(low), 4),
        "air_band_energy_ratio": round(float(air), 4),
        "spectral_centroid_hz": round(centroid, 1),
        "spectral_flatness": round(flatness, 6),
        "speech_modulation_ratio_2_8hz": round(speech_mod, 4),
    }


def analyze(path, ref):
    stereo, sr = decode_f32(path)
    mono = stereo.mean(axis=1).astype(np.float64)
    mid = mono
    side = ((stereo[:, 0] - stereo[:, 1]) / 2.0).astype(np.float64)
    duration = len(mono) / sr

    times, rms = frame_rms(mono, sr)
    rms_db = np.array([db(x) for x in rms])
    segment = max(1, len(rms_db) // 3)
    early = float(np.mean(rms_db[:segment]))
    middle = float(np.mean(rms_db[segment : 2 * segment]))
    late = float(np.mean(rms_db[2 * segment :]))
    quiet_threshold = np.median(rms_db) - 12.0
    quiet_share = float(np.mean(rms_db < quiet_threshold))

    _, mid_env = frame_rms(mid, sr, 0.10, 0.05)
    _, side_env = frame_rms(side, sr, 0.10, 0.05)
    valid = (mid_env > 1e-6) | (side_env > 1e-6)
    if valid.sum() > 3 and np.std(mid_env[valid]) > 1e-9 and np.std(side_env[valid]) > 1e-9:
        mid_side_env_corr = float(np.corrcoef(mid_env[valid], side_env[valid])[0, 1])
    else:
        mid_side_env_corr = 0.0

    result = {
        "ref": ref,
        "file": path.name,
        "duration_s": round(duration, 3),
        **loudnorm(path),
        "sample_peak_dbfs": round(db(np.max(np.abs(stereo))), 2),
        "rms_dbfs": round(db(np.sqrt(np.mean(stereo.astype(np.float64) ** 2) + 1e-12)), 2),
        "crest_factor_db": round(db(np.max(np.abs(stereo)) / (np.sqrt(np.mean(stereo.astype(np.float64) ** 2)) + 1e-12)), 2),
        "side_to_mid_db": round(db(np.sqrt(np.mean(side ** 2) + 1e-12) / (np.sqrt(np.mean(mid ** 2) + 1e-12) + 1e-12)), 2),
        "stereo_corr": round(float(np.corrcoef(stereo[:, 0], stereo[:, 1])[0, 1]), 4),
        "mid_side_envelope_corr": round(mid_side_env_corr, 4),
        "window_loudness_p10_dbfs": round(float(np.percentile(rms_db, 10)), 2),
        "window_loudness_p50_dbfs": round(float(np.percentile(rms_db, 50)), 2),
        "window_loudness_p90_dbfs": round(float(np.percentile(rms_db, 90)), 2),
        "quiet_window_share": round(quiet_share, 4),
        "early_mean_dbfs": round(early, 2),
        "middle_mean_dbfs": round(middle, 2),
        "late_mean_dbfs": round(late, 2),
        "late_vs_early_db": round(late - early, 2),
        **tempo_and_spectrum(mono, sr),
    }
    return result


def main():
    results = []
    for i, name in enumerate(FILES, 1):
        results.append(analyze(DOWNLOADS / name, i))
        print(f"analyzed ref {i:02d}", flush=True)
    payload = {"schema": 1, "analysis_sample_rate": 22050, "references": results}
    out = ROOT / "metrics" / "reference_audio_metrics.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
