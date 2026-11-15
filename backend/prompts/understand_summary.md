You are writing a short, factual summary of a hackathon app for the team that will cut its demo video.

Use only what the README, the source files and the recording's transcript below show. Do not invent features.{brief_block}

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
