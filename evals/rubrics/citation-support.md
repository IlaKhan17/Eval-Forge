# Does the cited passage support the keyed answer to this question?

You are shown a quiz **question**, the option keyed as **correct**, and the **passage** cited as
its source. Decide whether the passage supports that answer.

Answer `supported` when the passage contains the information a learner would need to select the
keyed answer over the alternatives.

Answer `unsupported` when the passage is about something else, is too general to distinguish the
keyed answer from the distractors, or contradicts it.

## Why this is a judge and `answer_correctness` is not

Whether the keyed answer is *factually correct* is human ground truth, and this rubric does not
ask it. A judge is exactly as likely to be wrong as the generator and correlated in its errors —
both are language models reasoning over the same passage — so a judge cannot be the sole arbiter
of factual correctness in a system that teaches people.

What a judge *can* do is check the narrower, more mechanical question of whether the cited text
is the right citation for that answer. That is a comparison between two pieces of text in front
of it, not a claim about the world.

## Boundary cases, decided

- Passage: "the derivative describes how a function changes." Keyed: "Derivatives describe how a
  function changes." → **supported**.
- Same passage, keyed: "Derivatives are always constant." → **unsupported**. Contradicted.
- Passage about integrals, question about derivatives → **unsupported**. Wrong citation, even
  though the keyed answer may well be correct.
