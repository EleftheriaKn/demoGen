You are shown one still frame from a screen recording of a hackathon app. The demo video will slowly zoom into one region of this frame while the voice-over says:

{lines}

Return the region the viewer should look at while hearing that: the part of the screen that shows what the line talks about (the list, the field, the counter, the button...).

- box_2d: [ymin, xmin, ymax, xmax], each 0 to 1000, relative to the frame (0,0 = top-left).
- Keep the region readable: include the whole element and a little margin, at least a quarter of the frame's width, and roughly the frame's shape (wider than tall).
- reason: one short line naming what the region shows.
