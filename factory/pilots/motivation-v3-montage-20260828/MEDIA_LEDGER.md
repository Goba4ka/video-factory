# Media ledger

| Slot | Speaker / asset | Verified input | Crop | Rights |
| --- | --- | --- | --- | --- |
| speaker-01 | Арсен Маркарян · B2 | 1920×1080 · 6.50s · SHA `c9cad427…` | x=360, y=0, 1080², no upscale | permission required |
| speaker-02 | Радислав Гандапас · B1 | 1920×1080 · 8.78s · SHA `c2f80f47…` | x=580, y=0, 1080², no upscale | permission required |
| speaker-02 video proxy | browser-safe native-crop CFR30/0.5s-GOP picture-only transcode of B1 | 1080×1080 · 8.78s; voice still read from original | x=580, y=0, 1080², no upscale | inherits permission required |
| speaker-03 | Алексей Ситников · B3 | 1920×1080 · 10.00s · SHA `b30979bf…` | x=270, y=0, 1080², no upscale | permission required |
| mono picture proxies | offline-baked from speaker-01/02/03, picture only | CFR30 H.264, frequent GOP, monochrome + restrained contrast | same approved crops; voice still from originals | inherits permission required |
| bed | Ref04 wide-pulse loop · 97 BPM | WAV · frozen project copy | n/a | permission required |

The source URLs, exact ranges, complete hashes, and publication gates are in
`source-ledger.json`. The technical inventory produced by `media-use --adopt`
lives under `.media/`.

The source contact sheets were inspected before fixing each crop. The stable
asset paths are the only paths the compositions reference.
