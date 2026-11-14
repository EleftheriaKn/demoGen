You check one claim from the voice-over of a demo video. Judge it only against the evidence below (README text, source files, and frames from the screen recording, attached in the order listed). Ignore anything else you know or guess about the app: if the evidence doesn't show it, it is not supported.

Claim: "{claim}"

Return:
- score: from 0 (the evidence does not show it) to 1 (the evidence clearly shows all of it).
- reason: one line naming what in the evidence backs it, or what is missing.
- action:
  - keep if the evidence shows the whole claim.
  - rewrite if it shows only part of it, and cutting words from the claim leaves a true statement.
  - drop if it shows none of it, or no cut makes it true.
- rewrite: for rewrite only, the claim with the unsupported words cut. Use only words already in the claim, in the same order, and add none. Empty for keep and drop.

Evidence:
<evidence>
{evidence}
</evidence>
