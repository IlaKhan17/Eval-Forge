# Is this meeting summary faithful to the transcript?

You are shown a **transcript** and a **summary** generated from it. Rate factuality 1 to 5.

- **5** — every statement in the summary is supported by the transcript.
- **4** — supported, but one detail is imprecise (a paraphrase that shifts emphasis).
- **3** — omits something material, but asserts nothing false.
- **2** — asserts one thing the transcript does not support.
- **1** — invents a commitment, an owner, or a date.

A summary that **invents a commitment** is the worst outcome here and scores 1 regardless of
how much else is correct. An action item nobody agreed to, attributed to someone who did not
agree to it, is worse than a summary that missed three real ones: the first creates a false
obligation, the second is merely incomplete.

Omission is scored more leniently than fabrication, deliberately. They are not symmetric
errors, and a rubric that treated them as one would let a hallucinated owner average out
against otherwise good coverage.
