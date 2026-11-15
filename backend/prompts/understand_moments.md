You are watching a recording of a hackathon project ({app}) so the team can cut its launch video. The recording may be a screen capture of the app, camera footage (the team, people using the app, a phone or device, the place it is used), or both.{brief_block}

The recording is {duration} long (MM:SS.s). Write every time as MM:SS.s, minutes and seconds from the start of the recording: 1 minute 13.5 seconds is "01:13.5", never "113" or "73.5". Split it into moments that cover it from start to end, in time order, without overlaps. Every second of the recording should belong to exactly one moment.

For each moment return:
- start, end: MM:SS.s from the start of the recording. end must be after start and no later than {duration}.
- description: one or two sentences describing only what is visible at the moment's end time (on the app's screen, or in the camera frame for camera footage): the final, settled state, not what is still being typed or about to happen. Name the exact filter, tab or view that is selected on screen at that time (for example the highlighted "Completed" filter, not "All"), and the items actually listed. Copy any text the app shows or the user types word for word, in double quotes, exactly as it appears on screen (same spelling, capitals and punctuation, including typos). Describe the state, not the change: write that "Fix login bug" is checked and the counter shows "3 items left", not that clicking it updated the counter or revealed a button. Do not describe anything you cannot see at the end time.
- feature_id: the feature from the list below that the moment shows, or null if none.
- kind, one of:
  - "result": the user does something and the moment runs until the outcome is on screen.
  - "action": the user is doing something in the app (typing, clicking, choosing).
  - "setup": navigation, loading, opening the app.
  - "context": real camera footage that doesn't show the app's screen clearly: the team, people using it, a phone or device, the place or problem it is for. Describe who and what is in frame. A filmed screen that you can read clearly counts as the app (result / action / setup), not context.
  - "dead": nothing useful happens (idle screen, mouse wandering, blank tab, waiting, blurry or empty camera shots).
- settled_at: for "result" moments, the time (MM:SS.s) at which the outcome is fully on screen and settled (the new item is in the list, the clicked filter is highlighted and the list has changed); null for other kinds. It must be after start. The moment's end should be just after settled_at.
- confidence: how sure you are about this moment, 0 to 1.
- quality: how well this stretch would play in a demo video, one of:
  - "wow": the payoff is clear on screen and settled long enough to read (the new item sits in the list, the filtered list has changed and stays still).
  - "ok": it shows what happens, but nothing stands out.
  - "weak": too fast to follow, messy (mistyped, undone, scrolling around, cursor hunting), or cut off before the result settles. Dead and setup moments are usually "weak" or "ok". For context moments, rate the shot itself: "wow" when it is steady, well framed and tells the story at a glance.
- flow_step: the step_id from the flow below that this moment shows on screen, or null if it shows none of them. Pick a step only if its outcome is visible in this moment.

An action and the result it causes are one moment: it starts when the user starts the action and ends once the result is visible on screen and has settled (about one second after it appears), never when the action starts or while it is still in progress. For example, typing a todo and pressing Enter is one "result" moment that ends after the new item shows in the list. Its description says the item is in the list only if it is in the list at the end time; if the moment ends while the text is still in the input box, end it later. Use "action" only when nothing visible comes of it.

A click or keypress counts only if you can see its result on screen before the recording ends. The cursor moving toward or resting on a button is not a click. If the recording ends before the result appears (for example a filter is pointed at but the list and the highlighted filter have not changed by {duration}), the last moment is "dead" and its description names the filter that is still highlighted.

Make a new moment whenever the kind changes. Keep idle stretches as their own "dead" moments rather than folding them into a neighbour.

Features:
{features}

Flow (the steps a user goes through in the app, in order, read from the repo; the recording may skip some or show them in another order):
{flow}
