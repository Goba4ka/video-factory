# Media provider expansion contract

Status: design-approved, adapters pending live acceptance. This document does
not turn a discovered URL into cleared media and does not authorize downloads.

## Why a second provider is required

Pexels is suitable for neutral stock B-roll, but it is not a dependable source
for dated war archives, named celebrities, quoted speakers, or clinical
illustrations. The factory therefore needs lane-aware provider routing while
keeping one immutable discovery → human rights review → byte freeze boundary.

## Provider matrix

| Lane | Default discovery | Secondary discovery | Fail-closed rule |
|---|---|---|---|
| war_history | Wikimedia Commons file namespace | approved public archives and museum collections | no fair-use inference; verify the exact file page, author, source, license, attribution, edit terms, depicted persons, symbols, territory, and expiry |
| celebrity_news | official press kit or rights-cleared agency feed | Pexels for generic establishing shots | social-platform availability and newsworthiness are not permission; named-person footage always goes to human rights/privacy review |
| motivation | owned or commercially licensed speaker recording | Pexels only for silent B-roll | original speaker audio requires consent/license evidence, exact time range, and source-byte hash; Fish TTS is forbidden in this lane |
| chinese_medicine | Pexels neutral B-roll | Wikimedia Commons diagrams after medical and rights review | avoid treatment demonstration without qualified medical approval; attribution and ShareAlike obligations remain attached to output metadata |
| health | Pexels neutral B-roll | Wikimedia Commons diagrams after medical and rights review | identifiable patients, dosage, children, pregnancy, or treatment claims require qualified human medical review |

## Wikimedia Commons adapter contract

The adapter may query only the official MediaWiki Action API on
`commons.wikimedia.org`. Search is restricted to namespace `6` (files). File
metadata is retrieved with `prop=imageinfo` and, for a bounded result set,
`iiprop=url|size|mime|mediatype|sha1|timestamp|extmetadata` plus an explicit
`iiextmetadatafilter`.

The official API documents that `extmetadata` is formatted metadata and is an
expensive property, so the adapter must cap results, cache the raw response,
record its SHA-256, and never issue an unbounded metadata query:

- https://www.mediawiki.org/wiki/API:Search
- https://www.mediawiki.org/wiki/API:Imageinfo
- https://www.mediawiki.org/wiki/Extension:CommonsMetadata

Each normalized candidate must preserve, without model rewriting:

- Commons file title and page ID;
- canonical file-description URL and original download URL;
- MIME/media type, dimensions, duration when available, upstream SHA-1 and
  retrieval timestamp;
- creator/artist, credit/source, license short name, license URL, usage terms,
  attribution text and raw metadata fields used to derive them;
- a cache receipt containing request parameters, response hash, fetch time and
  expiry.

Discovery must return `rights_cleared=false` and
`needs_human_review=true`. Missing creator, license URL, source, download URL,
or supported media type rejects the candidate. HTML in extended metadata is
treated as untrusted data and sanitized only for display; the raw value remains
in the immutable receipt.

## Rights boundary

Wikimedia Commons states that files can have different attribution and license
requirements, that Wikimedia does not warrant the accuracy of each copyright
status, and that personality, privacy, trademark, moral-rights and other
non-copyright restrictions can still apply. Therefore the adapter cannot mark
an item production-approved merely because Commons hosts it:

- https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia/en
- https://commons.wikimedia.org/wiki/Commons:Licensing/en

The rights reviewer chooses one exact license where multiple licenses exist,
records how attribution will be displayed, decides whether ShareAlike is
compatible with the output, and records any model/property/personality-rights
evidence. A direct file URL is eligible for freezing only after that
attributable approval. Redirects, MIME, maximum bytes and the downloaded hash
are independently checked by the media worker.

## Live acceptance

Before enabling a provider in the production registry:

1. Run one cache-miss and one cache-hit test without secrets in logs.
2. Prove request budget, timeout, redirect, response-size and domain allowlists.
3. Normalize at least one video and one still-image record, then reject an
   unsupported/missing-license fixture.
4. Complete attributable rights review for the exact candidate.
5. Freeze bytes, verify the hash and bind the attribution obligation into the
   render/publish metadata.
6. Re-run the same job from the cache and prove identical normalized output.

Until all six steps pass on the target host, the provider is `discovery_only`
and must not contribute to a production-ready claim.
