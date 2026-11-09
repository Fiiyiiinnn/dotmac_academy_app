---
chapter: 2
title: "Planning the Work"
part: "Setting Up"
---

# Planning the Work

A plan is not a prediction; it is a shared understanding of the work,
built so that surprises arrive early instead of late. The difference
between teams that plan and teams that don't isn't that planners avoid
problems — it's that planners meet their problems while there is still
time to respond. This chapter covers breaking work down, estimating like
an adult, sequencing around dependencies, and building the schedule you
can actually defend.

**By the end of this chapter you can:**

- Break work down until the pieces are small enough to estimate honestly.
- Estimate in ranges with the people doing the work, checked against history.
- Map dependencies and compute a critical path with slack.
- Say where a day saved changes the finish date and where it changes nothing.
- Place milestones that give the schedule a heartbeat.

FIGURE `MGT-PM-02` *Work broken into blocks, dependencies made visible — the plan is the shared picture, not the prophecy.*

## Break it down until you can see it

Nobody can estimate or manage "set up the new branch." The first planning
act is decomposition: break the outcome into deliverables, and
deliverables into tasks, until each task is small enough that one owner
can say what "done" looks like and roughly how long it takes — for most
projects, tasks of a few days at most. The test of a good breakdown is
coverage, not elegance: walk the list against the outcome and ask *"if
every one of these is done, is the project done?"* The gaps you find in
that walk-through are the cheapest gaps you will ever fix.

Two disciplines while decomposing:

- **Include the invisible work.** Approvals, training, testing, moving,
  cleaning up, documenting. Plans overrun less because tasks took longer
  than because whole categories of work were never on the list.
- **One owner per task** — the Team Leadership course's rule, applied at
  plan level. Tasks owned by "the team" are owned by nobody, and the plan
  should show a name beside every block.

## Estimating: honest numbers beat brave ones

Estimates go wrong in one direction, always the same one, because they're
built by imagining the work going well. The correctives:

- **Estimate with the person doing the work**, not for them. They know
  the snags; they also commit harder to numbers they gave (the Decision
  Making course's ownership effect). Where they're new to the task, add
  the learning time openly instead of hoping.
- **Use ranges for the uncertain.** "Three to six days depending on what
  we find in the wall" is information; "four days" is false precision.
  Plan against the realistic middle, and know which tasks carry the wide
  ranges — they're your risk list forming (Chapter 5).
- **Check against history.** The last comparable job took how long,
  actually? Records beat memory, and memory beats imagination. If the
  last three similar tasks each took double their estimate, the problem
  is the estimating, and the fix is arithmetic, not optimism.
- **Add contingency openly, at project level.** A visible buffer —
  commonly a tenth to a fifth of the total, more for novel work — owned
  by the project lead and spent on the surprises that always come. Hidden
  padding inside every task estimate does the same job dishonestly and
  teaches everyone the numbers are fiction.

## Dependencies: the order the work insists on

Some tasks can happen anytime; others wait — for another task, a
delivery, an approval, a season. Mapping dependencies ("plastering waits
on wiring; training waits on the system being ready") does two things.
First, it reveals the **critical path**: the chain of dependent tasks
whose combined length sets the earliest possible finish. A delay on the
critical path delays the project one-for-one; a delay elsewhere just
consumes slack. Knowing which is which tells you where to spend your
attention — and where a day saved actually matters.

Second, it exposes the **external dependencies** — the supplier delivery,
the permit, the sign-off from another department — which deserve special
paranoia because you don't control them. Chase them earlier than feels
polite: the approval that "takes two weeks" takes two weeks *from when
it's submitted*, and projects routinely lose a month by submitting late
paperwork punctually.

### Worked example: finding the critical path

A small installation, seven tasks, durations in working days:

| Task | Days | Waits for | Starts | Finishes | Slack |
| --- | ---: | --- | ---: | ---: | ---: |
| A · Survey the site | 3 | — | 1 | 3 | **0** |
| B · Place the materials order | 2 | A | 4 | 5 | **0** |
| C · Materials delivery *(supplier)* | 10 | B | 6 | 15 | **0** |
| D · Prepare the trench | 4 | A | 4 | 7 | 8 |
| E · Lay cable | 5 | C, D | 16 | 20 | **0** |
| F · Splice and test | 3 | E | 21 | 23 | **0** |
| G · Customer training | 2 | A | 4 | 5 | 18 |

The critical path is **A → B → C → E → F = 23 days**, and it is the only
chain with no slack anywhere on it. Three practical readings follow, and
none of them is obvious from a task list.

**Trenching faster buys you nothing.** D has eight days of slack: it finishes
on day 7 and is not needed until day 15. Send two extra people and finish it
in two days instead of four, and the project still ends on day 23. This is
the single most common waste in project management — visible effort applied
to the task that is easiest to speed up rather than the one that governs the
finish.

**The longest item on the critical path is the one you don't control.** C is
ten of the twenty-three days and it belongs to a supplier. That is where the
attention goes: confirm the lead time before planning around it, order
earlier than feels necessary, and get a delivery date in writing. A day lost
on C is a day lost on the project, one for one.

**Slack is not spare capacity — it is protection.** D's eight days are what
absorb the trench hitting rock. Spend them on comfort ("we've got time, start
D next week") and the slack is gone before the risk arrives. Slack is
insurance you have already bought; don't sell it back for convenience.

One caution: the critical path moves. If the supplier delivers early or the
trench hits that rock and takes fourteen days instead of four, D becomes
critical and C stops being. Recalculate after any significant change,
because managing yesterday's critical path is how projects overrun while
everyone watches the wrong task.

## Milestones: the heartbeat of the schedule

A milestone is a checkpoint that is unambiguously done or not done —
"equipment installed and tested", "approval received", "staff trained".
Spread them so that no long stretch passes without one, and make each an
honest binary: a milestone that can be "90% done" is a task wearing a
sash. Milestones are how everyone — sponsor included — tracks the project
without reading the whole plan, and how slippage announces itself early:
when the first milestone lands two weeks late, the plan is telling you
about the last one.

Build the schedule from the breakdown, the estimates, the dependencies,
and the *real* availability of the people (their day jobs, leave, and the
other project — availability is a planning fact, per Chapter 1). Then
resist the classic sponsor conversation in which the honest schedule is
compressed to a wished-for date by decree. A shorter date is buyable —
with more people, less scope, or more risk, chosen explicitly (the
scope–time–cost triangle again). Compression by decree just relocates
the truth to the end of the project, where it costs the most.

## Do this at work

1. **Break one task down.** Take the vaguest item on your plan and split it
   until each piece is something one person could finish in a few days. The
   vagueness usually hides the risk.
2. **Re-estimate with the doer.** Take an estimate you made for someone else
   and ask them for theirs. Where they differ, ask what they can see that you
   could not.
3. **Check three estimates against history.** How long did the last three
   comparable jobs actually take? If they consistently ran over, the fix is
   arithmetic, not encouragement.
4. **Draw your critical path.** List tasks, durations, and what each waits
   for. Find the longest dependent chain. Then find the tasks with slack and
   stop worrying about them.
5. **Find your longest external item.** The delivery, permit or sign-off you
   do not control. Confirm its real lead time in writing this week.
6. **Make the contingency visible.** If your plan has padding, move it into
   one named project-level buffer. Hidden padding teaches everyone the
   numbers are fiction.

## Summary

- Decompose until each task has one owner and a visible "done"; test the
  breakdown by walking it against the outcome, and include the invisible
  work — approvals, testing, cleanup.
- Estimate with the doers, in ranges where uncertain, checked against
  history, with contingency held openly at project level instead of
  hidden padding.
- Map dependencies to find the critical path — delays there hit the end
  date one-for-one — and chase external dependencies earlier than feels
  polite.
- Use binary milestones as the schedule's heartbeat; early milestone
  slippage is the plan warning you about the ending.
- A shorter schedule is bought with scope, people, or risk — never by
  decree; compression without trade-offs relocates the truth to the
  expensive end.
