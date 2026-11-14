# C2 test take: TodoMVC

| | |
|---|---|
| App | TodoMVC, plain JavaScript (ES6) version, https://todomvc.com/examples/javascript-es6/dist/ |
| Repo | https://github.com/tastejs/todomvc (`repo.txt`) |
| `take.mp4` | 3:03, 1920×1080, 30 fps, H.264, **no audio** |
| `take_1min.mp4` | first 60 s of `take.mp4` (setup → add todos → complete two → filters) |
| How | Scripted Chrome session (`record.js`), deliberately rough: typos fixed by backspace, idle stretches, a wandering mouse. Re-record with `NODE_PATH=<dir with puppeteer-core>/node_modules node record.js <out_dir>`, then encode the frames with ffmpeg (see the end of `record.js`). |

No voice: this is also the "no audio" case for B8. A take with voice-over is still worth
recording by hand later.

## What happens when (ground truth for checking `moments.json`)

Times in seconds, ±1 s.

| t | Kind | What's on screen |
|---|---|---|
| 0 | dead | Blank tab |
| 4 | setup | TodoMVC opens, empty list |
| 12 | action | Types four todos ("Buy milk", "Finish the hackathon demo video", "Call mom", "Fix login bug"), two typos fixed with backspace |
| 30 | result | Four todos, "4 items left" (newest at the top) |
| 33 | dead | Idle, mouse wandering |
| 42 | action | Ticks two todos |
| 46 | result | Two crossed out, "2 items left" |
| 49 | action | Filters: Active, Completed, All |
| 59 | result | Each filter shows the right list |
| 59 | dead | Idle |
| 69 | action | Double-clicks "Call mom", edits it to "Call mom (2 min max)" |
| 73 | result | Todo renamed |
| 76 | action | Hovers a todo, deletes it with × |
| 78 | result | Todo gone |
| 81 | dead | Idle |
| 90 | action | Adds four more ("Book flights", "Water the plants", "Reply to Nikos", "Pay electricity bill") |
| 103 | result | Seven todos |
| 105 | action | Mark all as complete |
| 109 | result | All crossed out, "0 items left", "Clear completed" shows |
| 114 | action | Undoes it, ticks three |
| 119 | action | Clear completed |
| 123 | result | Completed todos removed |
| 123 | dead | Idle |
| 133 | action | Adds "Submit before 18:00" |
| 139 | result | New todo at the top, counter updated |
| 141 | dead | Idle until the end (3:03) |

Not shown: the hosted demo did **not** keep todos after a page reload in our tests, so the
take makes no claim about saving. A good truth-check test: a script line like "your todos are
saved" should be rejected.
