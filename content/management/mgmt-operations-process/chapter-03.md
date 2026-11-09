---
chapter: 3
title: "Bottlenecks, Flow & Quality at Source"
part: "Improving the Work"
---

# Bottlenecks, Flow & Quality at Source

Two ideas do most of the work in operations improvement, and both are
counter-intuitive enough that teams get them backwards by default: **only the
constraint sets your output**, and **checking quality at the end is the most
expensive place to find a defect.** This chapter is about both.

**By the end of this chapter you can:**

- Find the constraint in a process, and say why improving anything else is
  wasted.
- Recognise what a bottleneck looks like from either side.
- Explain why large batches slow a process down.
- Put a figure on rework, and say where the defect should have been caught.
- Build a check into the step rather than after it.

FIGURE `MGT-OP-03` *Only the narrowest point sets the rate — everything upstream of it just makes a bigger queue.*

## Only the constraint sets output

In any sequence of steps, one step has the least capacity. **That step sets the
output of the whole process**, and improving any other step changes nothing —
it only builds inventory in front of the constraint or idle time behind it.

This is the same lesson as the Project Management course's critical path, one
level down: effort applied where there is slack produces visible activity and
no additional output.

Finding it is usually easy once you look for the right signs:

- **Work piles up in front of it** — a queue of jobs, a backlog of requests,
  a stack of paperwork waiting.
- **The step after it is sometimes idle**, waiting for input.
- **It is where everyone says "we're waiting on…"**.
- **Expediting happens around it** — the ad-hoc phone call to jump the queue,
  which is a bottleneck with a workaround attached.

Then, in order: **use it fully** before spending anything. Make sure the
constraint is never idle for a reason you control — no waiting for materials,
no doing work that could be done elsewhere, no breaks in coverage during which
it stops. Only then consider adding capacity, which is the expensive option
teams reach for first.

One caution: **the constraint moves.** Relieve it and a different step becomes
the limit. A team that keeps optimising last quarter's bottleneck is doing the
Project Management course's mistake of managing yesterday's critical path.

## Batches make queues

Large batches feel efficient — fewer setups, fewer trips, less switching — and
they slow the process down in a way that does not show up in any single step's
numbers.

The mechanism: if you process fifty jobs as one batch, the first job finished
waits for the other forty-nine before the batch moves on. Every job in the
batch inherits the duration of the whole batch. Cut the batch to ten and the
first jobs reach the next step five times sooner, with the same total work
done.

That is why a process can be busy at every step and still have terrible
end-to-end times. Each step is efficient; the *job* is waiting.

The practical instruction: **make batches as small as the setup cost allows**,
and if the setup cost is what forces large batches, attack the setup. Reducing
changeover time is usually the higher-return project, and it is almost always
the one nobody has looked at.

## Worked example: what rework costs, and where to catch it

An installation operation completes 500 jobs a month. Twelve percent need a
return visit, at ₦9,000 each.

| | |
| --- | ---: |
| Rework jobs per month | 60 |
| Cost per month | **₦540,000** |
| Cost per year | **₦6,480,000** |
| Halving the rate to 6% saves | **₦3,240,000 a year** |

Now the more useful question: **where was the defect created, and where was it
found?** Cost rises sharply with the distance between those two points.

| Caught at | What it costs |
| --- | --- |
| The step that created it | Minutes — correct it in place, nothing else has been built on it |
| The next step | The correction, plus the handoff back |
| Final inspection | The correction, plus everything done in between |
| **The customer** | All of the above, plus a return visit, plus the relationship |

Twelve percent found at the customer is the most expensive version of that
table. The same twelve percent found at the creating step would be a rounding
error.

Two honest qualifications. Not all rework is preventable at source — some
faults are genuinely only visible once assembled or energised. And driving the
rate to zero is rarely economic; the last few percent usually cost more than
they save. The gain is in moving detection **earlier**, which is cheaper than
eliminating the defect entirely and available immediately.

## Quality at source

Which leads to the principle: **build the check into the step, performed by the
person doing the work, rather than inspecting afterwards.**

Final inspection is the weakest form of quality control. It finds defects at
the point of maximum accumulated cost, it does not tell you why they happened,
and — the effect people miss — it quietly transfers responsibility away from
the person doing the work. If someone else checks it later, the incentive to
get it right first time weakens. That is not a character flaw; it is what the
system is arranging.

What works better:

- **A check at the step**, in the standard work of Chapter 2, done by the
  person doing the job.
- **Make the defect impossible where you can.** A connector that only fits one
  way, a form that will not submit incomplete, a fitting that cannot be
  installed backwards. This is the elimination level of the safety hierarchy,
  applied to quality — it beats every check because it needs nobody to
  remember.
- **Stop and fix rather than pass it on.** Passing a known defect downstream
  guarantees a more expensive correction later, and teaches everyone that the
  standard is negotiable.
- **Feed defects back to where they were created**, fast and without blame.
  Without the feedback the creating step cannot improve, and the same defect
  arrives again next week.

## Do this at work

1. **Find your constraint.** Look for the queue, the idle step behind it, and
   the place people expedite around.
2. **Check the constraint is never idle for a reason you control** — before
   spending anything on capacity.
3. **Recheck it after any improvement.** The constraint moves, and optimising
   last quarter's is wasted effort.
4. **Halve one batch size** and measure end-to-end time, not step efficiency.
   If setup cost forbids it, attack the setup instead.
5. **Cost your rework.** Volume × rate × cost per instance × twelve. It is
   usually larger than expected and it is never on a report.
6. **For your ten most recent defects, ask where each was created and where
   it was found.** The gap is your improvement target.
7. **Move one check earlier** — into the step, done by the person doing the
   work — and see what it catches.

## Summary

- Only the constraint sets output; improving anything else builds queues or
  idle time. Use the constraint fully before adding capacity, and recheck it
  after every improvement because it moves.
- Large batches make every job inherit the duration of the whole batch, which
  is how a process can be efficient at each step and slow end to end. If
  setup cost forces big batches, attack the setup.
- Rework cost rises with the distance between where a defect is created and
  where it is found; moving detection earlier is cheaper than eliminating the
  defect and available immediately.
- Final inspection is the weakest control: maximum accumulated cost, no
  explanation, and it transfers responsibility away from the person doing the
  work.
- Build the check into the step, make the defect physically impossible where
  you can, stop rather than pass it on, and feed defects back fast and
  without blame.
