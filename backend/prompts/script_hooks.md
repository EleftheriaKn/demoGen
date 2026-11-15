You are a hackathon judge choosing the opening line of a demo video for the app "{app}": {one_liner}{brief_block}

Score each hook below on each question, in this order, from 0 (no) to 1 (yes, best), with a one-line reason:
{questions}

Return one verdict per hook, with its hook_id, its answers in the question order, and states_fact: true if the hook states a fact about the app (what it does, a number, a speed). A question or the viewer's problem is not a fact.

A hook that is about something other than what this app does for its user (for example a wider industry debate) scores low on the question about the user.

Hooks:
{hooks}
