from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


DEFAULT_SITE_PACKAGES = (
    Path.home() / "AppData" / "Local" / "VideoFactoryRuntime" / "python"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe multiple media files with one Faster-Whisper model load."
    )
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default="ru")
    parser.add_argument("--site-packages", type=Path, default=DEFAULT_SITE_PACKAGES)
    args = parser.parse_args()

    sys.path.insert(0, str(args.site_packages))
    from faster_whisper import WhisperModel  # type: ignore

    args.out_dir.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    for input_path in args.inputs:
        output_path = args.out_dir / f"{input_path.stem}.transcript.json"
        segments, info = model.transcribe(
            str(input_path),
            language=args.language,
            beam_size=5,
            vad_filter=True,
            word_timestamps=True,
        )

        payload = {
            "source": str(input_path),
            "language": info.language,
            "language_probability": info.language_probability,
            "duration": info.duration,
            "segments": [],
        }
        for segment in segments:
            payload["segments"].append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text.strip(),
                    "words": [
                        {
                            "start": word.start,
                            "end": word.end,
                            "word": word.word,
                            "probability": word.probability,
                        }
                        for word in (segment.words or [])
                    ],
                }
            )

        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "source": str(input_path),
                    "output": str(output_path),
                    "segments": len(payload["segments"]),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
