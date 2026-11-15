You check one claim from the voice-over of a demo video. Judge it only against the evidence below (README text, source files, and frames from the screen recording, attached in the order listed). Ignore anything else you know or guess about the app: if the evidence doesn't show it, it is not supported.

Frames are still images from a screen recording, in time order. A claim about something the user does (adds, types, clicks, completes, filters, opens) is shown when the frames show its result on screen: the new item in the list, the item checked, the filter highlighted and the list changed. No still can show the motion itself, so don't drop a claim only because the action is not visible; drop it when its result is not visible either.

Claim: "{claim}"

Return:
- score: from 0 (the evidence does not show it) to 1 (the evidence clearly shows all of it).
- reason: one line naming what in the evidence backs it, or what is missing.
- action:
  - keep if the evidence shows the whole claim.
  - rewrite if it shows only part of it, and cutting words from the claim leaves a statement the evidence fully shows.
  - drop if it shows none of it, or no cut leaves a fully shown statement. If any part of your rewrite would still be unsupported, choose drop, not rewrite.
- rewrite: for rewrite only, the claim with the unsupported words cut. Use only words already in the claim, in the same order, and add none. Empty for keep and drop.
- rewrite_supported: for rewrite only, true if the evidence shows every part of the rewrite. False for keep and drop.

Evidence:
<evidence>
{evidence}
</evidence>
