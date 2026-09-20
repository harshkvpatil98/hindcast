# Cascade graph audit rubric (spec §5.4)

Score each sampled graph on three axes, 0 to 2. Write the score and a one-line
justification. The mean across all axes and all graphs must be at least 1.5;
below that, the compiler prompt is the problem and simulation tuning will not
fix it.

Judge the graph against the situation **as it stood at the cutoff**, using only
what was knowable then. You may know how the question resolved. That knowledge
makes a graph look wrong when it is merely uncertain, so set it aside: the
question is whether a domain-literate person at the cutoff would recognise this
decomposition, not whether it points at the answer.

## actor_completeness

Are the actors the ones a domain-literate person would name?

  0  The actor list is generic or wrong. Parties are placeholders ("regulator",
     "company") where specific institutions were identifiable, or several
     listed parties had no role in this situation.
  1  The principal parties are present but the list is thin or padded: an
     obvious secondary party is missing, or several actors are duplicates of
     one another under different names.
  2  The list is one a domain expert would recognise, with the principals
     present and each listed party plausibly involved.

## edge_sign_correctness

Are the influence signs right, judged by mechanism rather than by outcome?

  0  Multiple signs are backwards, or the signs appear to have been chosen to
     make the outcome rule point at the known answer.
  1  Mostly right, with one or two edges whose direction is questionable or
     which depend on an unstated assumption.
  2  Every edge's direction is defensible from the mechanism it represents.

## missing_leverage

Is anything with real leverage absent from the graph entirely -- not just as an
actor, but as a factor or an influence path?

  0  A decisive lever is missing. The graph could not produce the range of
     outcomes the situation actually admitted.
  1  The main levers are present but a material one is absent or is folded into
     another factor where it should move independently.
  2  Nothing with real leverage is missing; the graph spans the situation's
     plausible dynamics.

## Recording scores

Fill in `scores` in the worksheet JSON. Each entry needs an integer 0-2 for
each axis and a short `note`. Leave `note` empty only when the score is 2 and
there is genuinely nothing to say.
