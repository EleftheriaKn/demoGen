You are writing the voice-over script for a hackathon demo video of the app below. Write it in language "{language}".

The video tells one story in this order of beats: hook, problem, 2 to 4 demo shots, how, impact, ask. It aims for 75 seconds, about 190 spoken words in total. Keep each beat inside its word budget:
{budget}

Judges score on these criteria: {criteria}.

Return:
- hooks: exactly three different opening lines (one sentence each, within the hook budget). Make them specific to this app, for example the viewer's problem as a question. A hook makes no factual claim about the app: no feature, number or speed.
- hook_card_prompt: a prompt for a bold title card shown under the hook: the app's name over a background in the app's style.
- problem, demos, how, impact: footage shots. Each has:
  - voice_line: exactly what the voice says.
  - segment_id: one segment id from the list below. Prefer segments with a high frame score. Never use a segment whose moment is "dead".
  - source_start, source_end: the clip to show, in recording seconds, inside that segment's span.
  - claims: every factual statement the voice line makes about the app (what it does, how fast, how many...). Each one will be checked against its evidence alone and cut from the line if the evidence doesn't show it, so only say what the evidence shows. Copy the words from the voice line. For each, list its evidence: {{"type": "footage", "ref": <segment_id>}}, {{"type": "readme", "ref": "README#<heading>"}} or {{"type": "file", "ref": <repo path>}}, using only the segments, headings and paths listed below. Give an empty list if nothing shows it.
- demos: 2 to 4 shots, one feature each, in the order a user would meet them. Show results on screen, not navigation.
- how: one shot on how it works under the hood.
- impact: one shot on what changes for the user.
- ask: the closing line (a call to try it or to vote for it) with a card_prompt for its end card, and its claims.

Use only what the summary, segments and repo below show. Do not invent features.

App summary:
<summary>
{summary}
</summary>

Segments of the recording (id, span in seconds, what happens, best frame score 0-1):
<segments>
{segments}
</segments>

README headings: {headings}

Repo files: {tree}
