# Is the personalisation in this email grounded in the evidence provided?

You are shown an email **body** and the **evidence** that was retrieved about the recipient.
Rate how well the personalisation is grounded, from 1 to 5.

- **5** — every specific detail about the recipient traces to the evidence, and the evidence is
  used to say something relevant rather than merely name-dropped.
- **4** — grounded, but the evidence is used shallowly (mentioned and then ignored).
- **3** — generic. Nothing false, nothing specific either; this email could go to anyone.
- **2** — contains a specific claim about the recipient that the evidence does not support.
- **1** — invents facts about the recipient, or contradicts the evidence.

## What this is not asking

**Not whether the email is persuasive, well written, or likely to convert.** Those are different
metrics with different rubrics. Grading them here would make this score a vague overall
impression, and an overall-impression score cannot regress meaningfully — it moves for reasons
nobody can attribute.

**Not whether the claims about the product are approved.** That is a deterministic set check and
already gated separately.

## Boundary cases, decided

- Evidence: "hiring three revenue-ops roles." Body references the hiring and connects it to
  manual CRM work → **5**.
- Same evidence, body says "saw you're growing" with no further use → **4**.
- Body has no recipient-specific content at all → **3**. Generic is weak, not dishonest.
- Body claims a funding round the evidence does not mention → **2**.
- Body names the wrong company or the wrong person → **1**.
