You are judging still frames from a screen recording of a hackathon app, to pick the frame that best represents each stretch of the demo video.

The app: {app}

You are shown {n} frames, in this order: {frames}.

For each frame, in the same order, return:
- note: one short line on what the frame shows.
- answers: one answer per question below, in the same order. Each has:
  - score: 0 to 1, where 0 = no and 1 = clearly yes.
  - reason: one short line on why.

Judge only what is visible in the frame. Questions:
{questions}
