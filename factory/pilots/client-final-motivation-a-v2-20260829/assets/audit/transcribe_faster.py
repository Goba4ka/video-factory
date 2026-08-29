import json
import sys
from pathlib import Path

from faster_whisper import WhisperModel


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: transcribe_faster.py INPUT_AUDIO OUTPUT_JSON")

    source = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(source),
        language="ru",
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=True,
    )

    normalized_segments = []
    flat_words = []
    for segment in segments:
        words = []
        for word in segment.words or []:
            record = {
                "start": round(float(word.start), 3),
                "end": round(float(word.end), 3),
                "word": word.word.strip(),
                "probability": round(float(word.probability), 5),
            }
            words.append(record)
            flat_words.append(record)
        normalized_segments.append(
            {
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "text": segment.text.strip(),
                "words": words,
            }
        )

    payload = {
        "engine": "faster-whisper",
        "model": "small",
        "language": info.language,
        "language_probability": round(float(info.language_probability), 5),
        "duration": round(float(info.duration), 3),
        "segments": normalized_segments,
        "words": flat_words,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), "words": len(flat_words)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
