import asyncio
import json
import sys
from pathlib import Path

from shazamio import Shazam


async def identify(paths: list[str]) -> None:
    client = Shazam()
    results: list[dict[str, object]] = []
    for value in paths:
        path = Path(value)
        try:
            payload = await client.recognize(str(path))
            track = payload.get("track") or {}
            results.append(
                {
                    "file": str(path),
                    "title": track.get("title"),
                    "artist": track.get("subtitle"),
                    "url": track.get("url"),
                    "genres": track.get("genres"),
                    "sections": track.get("sections"),
                }
            )
        except Exception as exc:
            results.append({"file": str(path), "error": f"{type(exc).__name__}: {exc}"})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(identify(sys.argv[1:]))
