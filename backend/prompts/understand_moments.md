You are watching a screen recording of a hackathon app ({app}) so the team can cut its demo video.

The recording is {duration} seconds long. Split it into moments that cover it from start to end, in time order, without overlaps. Every second of the recording should belong to exactly one moment.

For each moment return:
- start, end: seconds from the start of the recording (decimals allowed). end must be after start and no later than {duration}.
- description: one or two sentences describing only what is visible on screen at the moment's end time: the final, settled state, not what is still being typed or about to happen. Name the exact filter, tab or view that is selected on screen at that time (for example the highlighted "Completed" filter, not "All"), and the items actually listed. Copy any text the app shows or the user types word for word, in double quotes, exactly as it appears on screen (same spelling, capitals and punctuation, including typos). Describe the state, not the change: write that "Fix login bug" is checked and the counter shows "3 items left", not that clicking it updated the counter or revealed a button. Do not describe anything you cannot see at the end time.
- feature_id: the feature from the list below that the moment shows, or null if none.
- kind, one of:
  - "result": the user does something and the moment runs until the outcome is on screen.
  - "action": the user is doing something in the app (typing, clicking, choosing).
  - "setup": navigation, loading, opening the app.
  - "dead": nothing useful happens (idle screen, mouse wandering, blank tab, waiting).
- settled_at: for "result" moments, the second at which the outcome is fully on screen and settled (the new item is in the list, the clicked filter is highlighted and the list has changed); null for other kinds. It must be after start. The moment's end should be just after settled_at.
- confidence: how sure you are about this moment, 0 to 1.

An action and the result it causes are one moment: it starts when the user starts the action and ends once the result is visible on screen and has settled (about one second after it appears), never when the action starts or while it is still in progress. For example, typing a todo and pressing Enter is one "result" moment that ends after the new item shows in the list. Its description says the item is in the list only if it is in the list at the end time; if the moment ends while the text is still in the input box, end it later. Use "action" only when nothing visible comes of it.

A click or keypress counts only if you can see its result on screen before the recording ends. The cursor moving toward or resting on a button is not a click. If the recording ends before the result appears (for example a filter is pointed at but the list and the highlighted filter have not changed by {duration} seconds), the last moment is "dead" and its description names the filter that is still highlighted.

Make a new moment whenever the kind changes. Keep idle stretches as their own "dead" moments rather than folding them into a neighbour.

Features:
{features}
