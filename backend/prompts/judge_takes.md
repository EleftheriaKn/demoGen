You are picking the best of several generated drafts for a hackathon demo video.

What the drafts are for: {context}{brief_block}

You are shown {n} images, in this order: {takes}.

For each image, in the same order, return:
- note: one short line on what the image shows.
- answers: one answer per question below, in the same order. Each has:
  - score: 0 to 1, where 0 = no and 1 = clearly yes.
  - reason: one short line on why.

Judge only what is visible in the image. Questions:
{questions}
