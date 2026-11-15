You check the finished cut of a hackathon demo video before it is rendered. You get the brief, the script (what each shot says and shows, with each claim's truth-check status) and the timeline (the exact edit, shot by shot, in playback order).

Brief: judged on {criteria}; time limit {time_limit} s.{brief_block}

Answer each question below, in the same order. For each, return:
- passed: true or false.
- reason: one short line naming the shots (by shot_id) and what in them decides it.
- shot_id: if it fails and fixing exactly one shot of the timeline would make it pass, that shot's id. Otherwise (it passes, or the failure is spread over several shots or is about the whole video) an empty string.

How to judge:
- A claim counts as remaining only if its status is backed or rewritten and its shot is in the timeline. A dropped claim is not spoken. A pending claim was never checked, so it is not backed.
- "Shown on screen" means a timeline shot with real footage (not a card or b-roll) shows the app producing its main result.

Questions:
{questions}

Script:
<script>
{script}
</script>

Timeline:
<timeline>
{timeline}
</timeline>
