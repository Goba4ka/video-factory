# Scout/research runtime

`video-factory scout` is the read-only external discovery stage. It reads current,
keyless official endpoints and never approves a topic, downloads production media,
or publishes anything.

Current adapters:

- NASA News Releases RSS: primary/official discovery source;
- Wikimedia Featured Feed API: official discovery endpoint and secondary evidence;
- NOAA National Ocean Service Ocean Facts, NOS News, and NOS Newsroom RSS;
- USGS significant-earthquakes-for-the-past-30-days GeoJSON;
- ESA Space Science RSS;
- Library of Congress Latest News RSS and Folklife Today RSS.

The XML adapter accepts both RSS and namespaced Atom entries. Source selection is
round-robin with URL deduplication, so one large feed cannot consume the complete
slate. The bundled fail-safe contains more than 28 unique evergreen candidates;
all of them are marked stale and cannot pass research automatically.

The command emits parallel `ideas` and `claim_ledgers` arrays. Their entries validate
against `idea_card.schema.json` and `claim_ledger.schema.json`, joined by `idea_id`.
Every discovery ledger is deliberately fail-closed: `decision.passed=false`,
`needs_human_review=true`, and `script_usage=qualify`. A downstream research agent
must inspect the linked page and corroborate material claims before scripting.

## Commands

```powershell
$env:PYTHONPATH = "$PWD\factory\src"
python -m video_factory scout --date 2026-08-27 --limit 28 --export scout.json
python -m video_factory разведка --offline --cache-dir .video-factory-cache/scout
```

Network requests have a bounded timeout and 0–3 retries. Successful raw responses
are stored atomically in the local cache. A failed live request falls back to the
last parseable cache entry. If no cache exists, the runtime emits a small bundled
evergreen fallback with explicit `bundled` provenance and a refresh warning. The
`--offline` switch makes no network calls.

Topic routing uses complete tokens and phrases, not substrings. NOAA and USGS
default to `nature_animals`, NASA and ESA to `space_technology`, and Wikimedia and
Library of Congress to `people_culture`. Strong explicit evidence can override a
source default; for example, NASA Earth science, ocean, and ecology items route to
`nature_animals`.

The cache is transport evidence, not a rights receipt or an archived source. Feed
URLs are evidence candidates only; they never imply permission to reuse embedded
images or video.
