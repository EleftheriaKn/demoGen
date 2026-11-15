You are writing the voice-over script for a hackathon launch video of the app below. Write it in language "{language}". It is a short spot that stands on its own, like a product launch reel or a pitch video: it makes a viewer want the app in seconds. It is not a tutorial or a walkthrough: show the moments that sell the app, not every screen in order.

The video tells one story in this order of beats: {beats}. It aims for {target_s} seconds, about {words} spoken words in total. Use the time: fill each beat close to its word budget (a short script leaves dead air), but never go over it. Fill it with detail the segments, summary and repo actually show (what the user types, clicks and sees on screen, word for word), never with invented features or numbers. Word budgets:
{budget}

Format: a launch spot. One idea per shot. Shots follow the user's flow, in the order a user meets each step. The hook runs 2 to 4 seconds; each feature shot 3 to 6 seconds.

Judges score on these criteria: {criteria}.{brief_block}

Return:
- hooks: exactly three different opening lines (one sentence each, within the hook budget). Make them specific to this app, for example the viewer's problem as a question. A hook makes no factual claim about the app: no feature, number or speed.
- hook_card_prompt: a prompt for the background artwork of the title card shown under the hook, in the app's style (colours, mood, texture). We write the title on top ourselves, so describe only the artwork: no titles, app names, buttons, words, letters or logos.
- {footage}: the middle shots, real footage unless the generated-shot rules below allow an illustration. Each has:
  - voice_line: exactly what the voice says. It must fit the segment's "voice up to N words" limit below: whole sentences past the limit are cut from the end, so put the sentence that matters first.
  - segment_id: one segment id from the list below. Prefer segments with a high frame score. Never use a segment whose moment is "dead". Segments marked "context footage" are real camera shots (the team, people using the app, a device, the place), not the app's screen: use them for problem, how or impact shots, never for demos. A footage claim on one says only what its frames show (who, what, where); anything about the app needs a README heading or repo file as its evidence.
  - source_start, source_end: the clip to show, in recording seconds, inside that segment's span.
  - claims: every factual sentence in the voice line, one claim per sentence (what the app does, what the user does or sees, how it works, what it changes, how fast, how many...). Write short sentences with one fact each: a sentence with one unsupported detail is cut whole, so two short true sentences beat one long sentence. A segment's description can run ahead of its frames, so name only what the segment shows when it is done (the end state), not steps still in progress. Problem, demo, how and impact lines are plain statements, never questions: only the hook may ask a question. The problem line states the problem from the summary as one claim whose evidence is a README heading from the list below ({{"type": "readme", "ref": "README#<heading>"}}), worded close to that section's own text. Each one will be checked against its evidence alone and cut from the line if the evidence doesn't show it, so only say what the evidence shows. Copy the words from the voice line. Footage is checked from a few still frames of the segment, so word footage claims as what a frame shows (the list shows "Buy milk", the footer reads "2 items left", the "Active" filter is selected), not as motions between frames (types, clicks, presses Enter). README and file evidence is checked against that section's or file's text alone, so claim only what that text says, close to its own words. For each, list its evidence: {{"type": "footage", "ref": <segment_id>}}, {{"type": "readme", "ref": "README#<heading>"}} or {{"type": "file", "ref": <repo path>}}, using only the segments, headings and paths listed below. Give an empty list if nothing shows it.
  - callout (inside one claim, or empty): the words shown on screen during the shot, at most 5, a benefit or a fact visible on screen ("Edit in place", "2 items left"). Put it on the claim it restates, using that claim's own words where you can: it is checked with that claim and cut with it. At most one per shot; leave every callout empty if nothing short and true fits. The hook and the ask have none.
- demos: {demo_count}, each on a different segment and a different moment of the take, one feature each, ordered so the video builds to its strongest payoff (the best wow moment last), not a tour in menu order. Show results on screen, not navigation. Each demo line is one complete sentence (subject and verb, never a fragment like "Switch views.") that says what the user achieves, addressed to the viewer ("Filter to see only what's left: just Call mom and Buy milk."), not a flat description of the screen ("The list displays two items."). That sentence is the shot's one claim, with the shot's own segment as its evidence, and what the user achieves must be what the segment's end state shows. Don't open a demo line with a general feature sentence from the README.
{beat_lines}- generated shots (only when a user flow is given below; otherwise every middle shot is footage). Fill each middle shot's flow_step with the step id it covers ("s2", never a shot id) or "", and generated true or false:
  - A step shown by a moment of quality wow or ok is real footage on that moment's segment. Never generate it.
  - A step shown only by a weak moment, or a step marked MISSING (the take never shows it), is a generated demo shot. Give a generated demo shot (see the demo count) only to the steps that sell the app (its core payoff); skip routine steps (sign-up, settings, navigation) and place the shot where it serves the story. Its claim says what the user can do at that step, worded close to a listed README section or repo file that describes it (a spec or the code); never cite a file that is not listed below. The demo rules above about segments, end states and footage evidence apply to footage demos only.
  - how and impact may be generated when the footage can't show them.
  - A generated shot: generated true; segment_id the segment of its weak moment, or of the nearest step the take does show ("" if none), source_start and source_end 0; card_prompt the picture to draw (the app's screen at that step as a flat vector illustration in the app's colours: no text, letters, numbers or logos, we write every word); claims backed only by README headings or repo files listed below, or the step's own evidence, close to their own words. Never footage evidence: nothing real is on screen.
  - A footage shot: generated false, card_prompt "".
  - At least half of the video's runtime stays real footage: generated shots past that are dropped.
  - If a judging brief is given above, let it steer which steps matter most and what the story stresses, but claim nothing from it.
- ask: the closing line, a plain call to try it or to vote for it (within the ask budget) that states no fact about the app, so its claims are empty; plus a card_prompt for the background artwork of its end card, described the same way as hook_card_prompt: artwork only, no titles, app names, buttons, words, letters or logos.

Use only what the summary, segments and repo below show. Do not invent features.

App summary:
<summary>
{summary}
</summary>

Segments of the recording (id, span in seconds, what happens, best frame score 0-1, most words its voice line can hold):
<segments>
{segments}
</segments>

{flow}

README headings: {headings}

Repo files you can cite (the checker reads their content): {tree}

Before you answer, check:
- Every voice line fits its segment's word limit, with the sentence that matters first.
- Every factual sentence is a claim with its evidence listed, and says only what that evidence shows: a footage claim names what a frame shows at the end (the list, the counter, the highlighted filter); a README or file claim stays close to that text.
- The hook and the ask state no facts.
- Demo lines: one feature each, building to the strongest payoff, each on its own segment, addressed to the viewer.
- It plays as a launch spot on its own: every shot earns its place, nothing reads like a tutorial.
- If a judging brief is given above, the story answers it: lead with what it rewards.
- Nothing is invented: no feature, number or speed that the summary, segments or repo don't show.
- Callouts: at most one per middle shot, at most 5 words, on the claim they restate; none on the hook or the ask.
