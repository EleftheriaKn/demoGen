You are writing a short, factual summary of a hackathon app for the team that will cut its demo video.

Use only what the README, the source files and the recording's transcript below show. Do not invent features.

Return:
- name: the app's name.
- one_liner: a one-sentence pitch.
- problem: the problem it solves, in one or two sentences.
- audience: who it is for.
- features: what a user does in the running app, most important first. Each feature is one thing the user can see and do on screen (for a todo app: add a todo, complete it, filter the list, clear completed). Do not list facts about the repository itself (tests, docs, examples, build tooling, CI, framework choice, contributor guides) as features; if the README is mostly about the project, read the app's behaviour from the spec, source files and transcript. For each:
  - feature_id: "f1", "f2", ... in order.
  - name: a short name.
  - description: one or two sentences on what it does for the user.
  - evidence: where the repo shows it. Each entry is either "README#<heading>", with the heading text exactly as it appears in the README (without the # marks), or a file path exactly as it appears in the file tree. Give at least one entry per feature.
- flow: the steps a user goes through in the running app, in the order they meet them, from opening the app to the last thing they do (for a todo app: open the app, add a todo, add another, complete one, filter to active, clear completed). Each step is one thing the user does or sees on screen; 4 to 10 steps. Use the README, spec and source to find the order (what the app shows first, what needs something done before it). For each:
  - step_id: "s1", "s2", ... in order.
  - text: what the user does or sees at this step, in one short sentence.
  - feature_id: the feature above this step uses, or null (for example opening the app).
  - evidence: where the repo shows this step, in the same form as a feature's evidence. Give at least one entry per step.

Use the judging brief below to know what the judges look for, but describe only what the repo shows.

Judging brief (pasted by the team; may be empty):
<brief>
{brief}
</brief>

README:
<readme>
{readme}
</readme>

File tree:
<tree>
{tree}
</tree>

Key files:
{key_files}

Transcript of the recording (the presenter talking while using the app; it may be rough):
<transcript>
{transcript}
</transcript>
