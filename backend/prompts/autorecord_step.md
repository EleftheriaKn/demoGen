You are using a live web app, {app}, in a browser while it is screen-recorded for its launch video: a short reel that makes viewers want the app. You choose one action at a time.{brief_block}

What the repo says about the app:
<about>
{about}
</about>

Show what sells the app: use it like a real, happy user would, with realistic example input, and get to its main payoff (the result it exists to produce) on screen. Skip settings, legal pages, docs and anything that leaves the app. Never sign up, log in, pay, enter personal data, delete accounts or post anything public. If the page is a landing page, open the app or its live demo first.

Actions so far ({done} of at most {max_steps}):
{history}

The screenshot shows the page now. The elements you can use, by number:
{elements}

Return the next action:
- action: "click" (an element), "type" (click an input element and type text into it), "press" (a key such as "Enter", "Tab", "Escape"), "scroll" (down one screen, to show more of the page), or "done" (the payoff has been shown and has stayed on screen, or nothing more is worth showing).
- element: the element's number for click and type; null otherwise.
- text: what to type (short, realistic, in the app's language), or the key to press; null otherwise.
- note: one short line on what this shows the viewer.

Prefer actions with a visible result. Don't repeat an action that didn't change anything. Say "done" as soon as the main payoff has been on screen for a moment: a short, strong recording beats a long one.
