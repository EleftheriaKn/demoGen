You check one claim from the voice-over of a demo video. Judge it only against the evidence below (README text, source files, and frames from the recording, a screen capture and/or camera footage, attached in the order listed). Ignore anything else you know or guess about the app: if the evidence doesn't show it, it is not supported.

Footage frames come in time order, each labelled: "before" is the last frame just before the clip, "during" are frames from the clip, "after" is the first frame just after it. Compare them, not one at a time: a still cannot show motion, but a change between frames shows an action. An action claim ("adds", "types", "clicks", "checks off", "filters", "clears") is shown when the frames show its result appear across that order, with at least one "during" frame showing the change happening or its result: an empty list, then the list with one item, shows "adds a todo"; an unchecked item, then the same item checked, shows "checks it off". A change the frames contradict, or one no frame shows, is not shown.

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
