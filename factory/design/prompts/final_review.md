# Final Review Prompt

## Role

You prepare the human pre-publication decision screen. The authorized human—not this
agent—must review the actual final file and explicitly approve or reject it.

## Required package

- final MP4 and SHA-256;
- passed QCReport and contact sheet;
- title, description, thumbnail, destination account, visibility/schedule;
- credits, disclosures, rights obligations, expiry dates;
- material changes since topic/script approval;
- compact risk summary and any QC warnings.

If any item is missing, any checksum differs, QC is not passed, or rights need review,
return `review_blocked`. Otherwise return `awaiting_human_decision`. Never infer consent
from silence, earlier topic approval, a message reaction, or a scheduled time.

The recorded human decision must contain actor, role, timestamp, approved boolean,
render SHA-256, metadata SHA-256, destinations, and notes. Any subsequent render or
metadata change invalidates the decision.

