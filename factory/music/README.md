# Lane music pools

`lane_music_catalog.json` separates five editorial music pools. It currently
contains ten concrete audio archetypes and twenty production slots: one
cross-platform and one TikTok-only slot for every archetype. All slots are
deliberately `pending_reference`; `tracks` is empty. No track name, licence, or
local asset has been invented.

## Reference fingerprint workflow

The nightly/manual research pass uses TikTok Creative Center Trends and Top Ads
plus manually verified Reels/Shorts observations. TikTok's official docs say
Trends can be filtered by industry/timeframe and exposes trendlines, related
videos, audience insights and regional popularity; Creative Center also exposes
high-performing ads and their successful engagement moments:

- <https://ads.tiktok.com/resources/help/article/how-to-use-trends?lang=en>
- <https://ads.tiktok.com/resources/help/article/creative-center?lang=en>

For each lane/archetype, record the source URL, market, observation window,
visible success metric, capture time and measured audio fingerprint: BPM, energy
curve, vocal presence, instrumentation and edit accents. A fingerprint is a
structural observation only. It is explicitly not licence evidence and never
authorizes copying, downloading, fingerprint-matching or reusing the recording.

## Turning a slot into a production track

A track may enter `tracks` only when the slot becomes `ready` and the record has:

- an absolute local PCM WAV path and exact SHA-256;
- a local licence receipt and exact SHA-256;
- creator, licence name/source/URL, commercial and modification rights;
- explicit `platform_scope`, `territories`, `placements`, expiry and attribution;
- a human approval bound to the canonical track-record checksum.

`video_factory.music_catalog.resolve_music_selection` rechecks all of this at
runtime and returns the exact lane/archetype/slot/track selection. The BGM
handler requires that rich selection, verifies it against RightsManifest and
FrozenMediaManifest, then records it in BgmManifest 1.2. Missing or stale
catalog evidence fails before normalization or mixing.

TikTok's Commercial Music Library can be filtered by region, usable placement,
theme, genre, mood and duration and is cleared for eligible TikTok usage:
<https://ads.tiktok.com/resources/help/article/how-to-use-the-commercial-music-library?lang=en-GB>.
The resolver therefore never expands a CML record to Instagram Reels or YouTube
Shorts. A cross-platform slot requires a separate independent commercial
licence covering every requested platform and placement.

The Chinese-medicine pool forbids gong, bamboo-flute, pentatonic and mystical
"Asian" clichés by default. A culturally specific instrument requires a real
story reason and still cannot be used as evidence of medical efficacy.

## Runtime input

Set `VIDEO_FACTORY_MUSIC_CATALOG` to the reviewed catalog file. Candidate input
must provide `bgm_selection` with the exact catalog id/version, track/asset/
archetype ids and requested platforms, territories and placements. The default
catalog is intentionally not production-resolvable until real licensed tracks
are approved.
