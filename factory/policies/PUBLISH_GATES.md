# Publish gates

Publishing is a separate authority. Topic, research, scripting, asset, and
render agents never hold account credentials.

All destinations require:

1. Approved IdeaCard.
2. Passed ClaimLedger.
3. Passed RightsManifest with no missing timeline asset.
4. RenderManifest with immutable input hashes.
5. QCReport with no failed or not-run blocking check.
6. Human semantic approval of the final frame sequence and narration.
7. Explicit decisions for AI/synthetic and commercial disclosures.
8. Platform-specific caption, music rights, visibility, and account selection.
9. A passed freshness gate. Default maximum fact-check age at publication is
   2 hours for `celebrity_news`, 24 hours for `health` and
   `chinese_medicine`, 168 hours for `war_history`, and 720 hours for
   `motivation`; a lane may impose a shorter TTL.
10. A passed visual-provenance review: archive, reenactment, simulation, stock,
    and AI-generated material is labeled whenever a reasonable viewer could
    mistake it for the reported event.
11. A passed originality review. Captions, borders, crops, speed changes, and
    color filters alone do not make third-party footage an original work; the
    output must add a new evidence-backed analysis, explanation, comparison, or
    storyline.

TikTok remains a human-confirmed posting step unless an audited integration
whose use case permits the intended workflow is in place. YouTube and Instagram
may be scheduled through approved official APIs after the same gates pass.

No agent automatically disputes a copyright claim, takedown, strike, or policy
decision. Those actions require a documented human decision and supporting
rights evidence.

See [ORIGINALITY_AND_PROVENANCE.md](./ORIGINALITY_AND_PROVENANCE.md) for the
minimum transformative-value and archive-labeling standard.
