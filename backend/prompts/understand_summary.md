You are writing a short, factual summary of a hackathon app for the team that will cut its demo video.

Use only what the README, the source files and the recording's transcript below show. Do not invent features.

Return:
- name: the app's name.
- one_liner: a one-sentence pitch.
- problem: the problem it solves, in one or two sentences.
- audience: who it is for.
- features: the app's features, most important first. For each:
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
