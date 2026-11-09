---
chapter: 3
title: "Root-Cause Problem Solving"
part: "Structured Methods"
---

# Root-Cause Problem Solving

Some problems come back. The same complaint every month, the same error every
quarter, the same argument every project. Recurrence is the signature of a
symptom being treated while its cause survives. This chapter is about the
discipline of digging past the first plausible answer — and knowing when the
digging is done.

**By the end of this chapter you can:**

- Tell a symptom from a cause, and a one-off from a class of problem.
- Establish facts before adopting a theory.
- Run five whys past the point where a person's name appears.
- Say what fix each level of "why" buys, and why workarounds are a tax.
- Verify a fix actually worked rather than assuming it did.

FIGURE `MGT-DM-03` *The symptom is the tip; the cause is the structure under the waterline.*

## Symptom, cause, and the class of problem

A **symptom** is what you observe: the late delivery, the angry customer,
the failed job. A **cause** is what produces it. The distinction matters
because effort spent on symptoms is rented relief — it must be paid again
each time — while effort on causes is purchased permanently.

The practical ambition is to fix the **class of problem, not the instance**.
The instance is *this* invoice being wrong; the class is *invoices of this
type can be wrong*. Instance-fixing is sometimes all you can afford today,
but a team that only ever instance-fixes runs on a treadmill: everyone is
busy, everyone is competent, and nothing improves.

## Facts before theories

Root-cause work fails at the start more often than at the end, because
people begin with a theory and collect evidence for it (Chapter 1's
confirmation bias, now in a work uniform). Reverse the order:

- **Go look.** Examine the actual failed item, the actual timeline, the
  actual records — not a summary of them. Second-hand descriptions smuggle
  in interpretations.
- **Write the timeline first.** What happened, in order, with times. Most
  "mysteries" thin out dramatically once sequence is established.
- **Separate observation from inference** in what people tell you. "The
  system was slow" is an inference; "my screen took a minute to load at
  9:40" is an observation. Ask for the observation behind each inference.

## The five whys

The simplest root-cause tool: ask "why?" repeatedly — typically around five
times — until you reach something structural.

> Delivery was late. **Why?** The van left two hours behind schedule.
> **Why?** Loading finished late. **Why?** The picking list wasn't ready.
> **Why?** The overnight job that produces it failed, and nobody noticed
> until morning. **Why?** The job's failure alert goes to a mailbox nobody
> reads.

Stopping at "the van left late" produces a talking-to for the driver.
Reaching the unread mailbox produces a fix that prevents a whole family of
future failures. Three cautions:

- **Don't stop at a person.** "Because Ada made a mistake" is never the
  floor. Ask why the mistake was *possible* and why it wasn't *caught*. If
  your root cause is a name, you have a symptom with feelings.
- **Branch when answers multiply.** Real problems often have two or three
  contributing causes. Follow each branch; don't force a single chain.
- **Stop at actionable.** The test for "deep enough": you've reached
  something you can change that would have prevented the problem. Beyond
  that lies philosophy ("why does entropy exist?") — pull back up one level.

### What each level of "why" buys you

The reason to keep asking is that the value of the available fix rises
sharply with depth. Same incident, five possible stopping points:

| Stop at | The fix available here | What it prevents |
| --- | --- | --- |
| The van left late | A word with the driver | Nothing. The driver was not the cause. |
| Loading finished late | Chase the loading bay harder | This one delivery, maybe |
| The picking list wasn't ready | A manual workaround each morning | Late loading — at the cost of someone's daily attention, for ever |
| The overnight job failed | Fix the job | This failure mode, until the next job fails |
| **The alert goes to an unread mailbox** | **Route alerts somewhere monitored** | **Every silent failure of every overnight job** |

Notice the third row, because it is the one real organisations actually
choose. A manual workaround genuinely stops the symptom, so it looks like a
solution — and it quietly converts a one-off defect into a permanent tax on
someone's morning. Workarounds are how organisations accumulate the "decision
debt" of Chapter 1: each is locally sensible, and collectively they are why
everyone is busy and nothing is fixed.

The last row is a different kind of fix. It does not just repair this
incident; it repairs the *class*. That is what "structural" means, and it is
why the extra two minutes of asking is the highest-return two minutes in
problem solving.

## The fishbone: when causes are tangled

When a problem is recurring and multi-causal — quality drifting, morale
sagging, a process failing intermittently — a chain of whys is too linear.
The **fishbone (Ishikawa) diagram** organises a broader search: the problem
at the head, and ribs for candidate cause categories — commonly *people,
process, equipment, materials, environment, measurement*. The team brainstorms
causes onto each rib, then tests the credible ones against evidence.

The fishbone's real function is *coverage*: it forces the group past its
favourite theory into categories nobody was defending. Pair it with a
**Pareto check**: when many causes contribute, rank them by frequency or
cost — typically a small handful accounts for most of the pain, and that is
where the fixing budget goes. You do not need to slay every rib of the fish;
you need the two ribs that cause 80% of it.

## Verify the fix, close the loop

A root-cause exercise ends with a fix that *someone verifies actually
worked*. That means:

1. **Predict.** If this is truly the cause, fixing it should measurably
   change something — name the measure before deploying the fix.
2. **Check on a date.** Recurrence is the test. Put a revisit note in the
   calendar: has the problem class reappeared since the fix?
3. **If it recurs, reopen honestly.** A returned problem means the real
   cause survived your fix. That is information, not humiliation — the
   second investigation starts far ahead of the first.

And close the loop with the people involved: the team that lived the problem
should hear what the cause was and what changed. It converts an annoyance
into visible improvement — and it trains everyone in the habit of asking
one more "why".

## Do this at work

1. **Run five whys on last week's incident.** Write the chain down. If you
   stop at a person's name, you have a symptom with feelings — keep going.
2. **Map the fixes by level.** For that chain, write what fix is available
   at each depth and what each would prevent. The leverage difference is
   usually stark.
3. **Find a workaround you are paying for.** Something someone does manually
   every day to compensate for a defect nobody fixed. Cost it in hours per
   year, then decide whether the real fix is cheaper.
4. **Separate the one-off from the class.** For a recurring issue, ask what
   family of failures the structural fix would prevent, not just this
   instance.
5. **Check a fix you already made.** Did the metric respond? Fixes are
   assumed to work far more often than they are verified, and an unverified
   fix is a belief.
6. **Ask what would prove you wrong.** For your current leading theory,
   name the evidence that would refute it — then go and look for it.

## Summary

- Fixing symptoms is rented relief; fixing causes — the class, not the
  instance — is bought permanently.
- Start from facts: go look, build the timeline, separate observation from
  inference. Theories come second.
- Use five whys past the first plausible answer; never stop at a person's
  name; branch when causes multiply; stop when you reach actionable.
- Use a fishbone for tangled recurring problems and Pareto to focus on the
  few causes carrying most of the cost.
- A fix isn't done until a named measure, checked on a named date, shows the
  problem class has stopped recurring.
