You are watching a screen recording of a hackathon app ({app}) so the team can cut its demo video.

The recording is {duration} seconds long. Split it into moments that cover it from start to end, in time order, without overlaps. Every second of the recording should belong to exactly one moment.

For each moment return:
- start, end: seconds from the start of the recording (decimals allowed). end must be after start and no later than {duration}.
- description: what happens on screen, in one or two sentences. Copy any text the app shows or the user types word for word, in double quotes, exactly as it appears on screen (same spelling, capitals and punctuation, including typos). Do not describe anything you cannot see.
- feature_id: the feature from the list below that the moment shows, or null if none.
- kind, one of:
  - "result": the payoff is on screen (the app shows the outcome of an action).
  - "action": the user is doing something in the app (typing, clicking, choosing).
  - "setup": navigation, loading, opening the app.
  - "dead": nothing useful happens (idle screen, mouse wandering, blank tab, waiting).
- confidence: how sure you are about this moment, 0 to 1.

Make a new moment whenever the kind changes. Keep idle stretches as their own "dead" moments rather than folding them into a neighbour.

Features:
{features}
