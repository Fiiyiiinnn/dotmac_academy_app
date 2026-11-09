---
chapter: 1
title: "Working in Sub: Your Role, Your Access"
part: "The Role"
---

# Working in Sub: Your Role, Your Access

Every call, chat, and email a customer sends about their connection ends up
in one place: **Sub** (`dotmac_sub`), the operator's record of who a
subscriber is, what service they have, and what has already been done for
them. This chapter is not about clicking buttons — it is about understanding
what Sub *is*, so the rest of the course makes sense: why you see the screens
you see, why your access stops exactly where it does, and why "just write it
in the group chat" is never a substitute for the ticket.

**By the end of this chapter you can:**

- State what Sub is the official record of, and why that matters.
- Name the screens you will use most and what each one is for.
- Explain why your account can do some things and not others — and why that
  is not a mistake to work around.
- Know where to look when something you need is not where you expected.

## Sub is the record, not just a tool

Other operators run support out of spreadsheets, WhatsApp groups, and
whoever remembers what happened last time. Dotmac does not: Sub is the
single place where a subscriber's status, service, billing state, and
support history live. If it did not happen in Sub — a status change, a
ticket, a note — it did not officially happen at all, no matter how clearly
you remember the phone call.

This cuts both ways. It means you can trust what you read in Sub instead of
calling around to confirm it. It also means every action you take there is
part of a real, permanent record: the "Activity Timeline" on a ticket does
not forget, and neither does anyone auditing it later. Write comments and
set statuses as if someone will read them next year, because someone might.

## Where you will spend your day

The left-hand navigation is smaller than it looks. As a support agent you
will live in a handful of areas:

- **Dashboard** — a daily overview: subscriber counts, overdue receivables,
  suspended accounts, anything flagged as needing attention.
- **Customers** — subscriber accounts: status, plan, billing, service history.
  Chapter 3 covers this in depth.
- **Support** — the ticket queue: intake, triage, assignment, resolution.
  Chapters 2, 4, and 5 live here.
- **Workqueue** — the work assigned specifically to you, cutting across
  tickets and follow-ups.
- **Inbox** — customer conversations (chat/email/social) that may or may not
  already be tickets. A conversation is not automatically a ticket, and a
  ticket is not automatically closed just because the conversation stopped.
- **Reports** — subscriber and support operations reporting, for when you
  need the pattern rather than the single case.

Everything else in the sidebar (Billing detail screens, network
infrastructure, system administration) belongs to other roles. That is
deliberate, not an oversight — which brings us to access.

## Why your access looks the way it does

Your account carries the **support** role, and that role is a specific,
deliberate bundle of permissions — not "whatever an agent might conceivably
need." Broadly, you can:

- Read and update customer accounts, and read subscriber reports.
- Read and manage subscriptions: activate, suspend, read status.
- Read billing: invoices, payments, credit notes, dunning state — and take
  a payment.
- Read, create, and update support tickets, and self-assign inbox
  conversations.
- View and edit ticket automation rules.
- Read CRM contacts (read-only), and read and write CRM conversations.

Notice what is missing: you cannot reach the **network monitoring** screens
that Network Operations uses to watch infrastructure health and declare
outages. That is not a gap someone forgot to fill in — it is a different
job with different risk, held by a different role. Chapter 4 shows you
exactly what that boundary looks like in practice, and — more usefully —
what you *can* do instead, because there is a real, supported way to work
with outage information without holding that permission yourself.

If a screen you expected is missing, the right response is never to find a
workaround that reaches it anyway. Ask whether the task actually belongs to
your role, and if you believe it should not be missing, say so through the
proper channel rather than improvising around it.

## Common failure modes

- **Treating the chat history as the record.** A clear conversation in the
  team channel is not a substitute for a ticket update. If it is not in Sub,
  the next person to open the ticket cannot see it.
- **Assuming your access is a bug.** A missing screen is almost always a
  boundary, not an oversight. Confirm before you go looking for another way
  in.
- **Writing internal notes like no one else will read them.** Shorthand that
  makes sense to you today is opaque to the next agent, and to whoever
  reviews the ticket in a complaint or audit.

## Do this at work

1. **Open the Dashboard and read every card once**, even the ones that do not
   look urgent. Knowing the shape of a normal day makes an abnormal one
   obvious faster.
2. **Open one ticket, start to finish**, and read the Activity Timeline at
   the bottom before doing anything else. This is what "the record" actually
   looks like.
3. **Find the Customers screen and open any account.** Do not change
   anything — just look at the layout you will be reading constantly.
4. **Try to name, from memory, three things your role can do and one thing
   it cannot.** If you cannot name the "cannot," re-read the permissions
   list above.
5. **Write down one question about your access** you are not sure about, and
   ask your supervisor rather than guessing.

## Practice scenarios

### Scenario: The WhatsApp shortcut

A colleague messages you: "Just message the customer on WhatsApp directly,
it's faster than opening a ticket." You do, and the customer's issue is
resolved in ten minutes flat — everyone is happy, and you move on to the
next thing in your inbox.

**What do you do?**

**A)** Nothing further — the customer's problem is solved, and that is what
matters.
**B)** Go back and create a ticket in Sub afterward, describing what
happened and how it was resolved, even though the work is already done.
**C)** Mention it to your supervisor verbally at the next team huddle, so
they know you handled something quickly.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. The customer is happy right now, but nothing about this
  interaction exists in Sub — the official record. The next agent who talks
  to this customer starts from zero, and if the same issue recurs across
  many customers, no one will see the pattern, because it never happened
  "officially" at all.
- **B)** Correct. Logging it in Sub *after the fact* is exactly the fix —
  resolving the problem quickly and having it exist in the record are not in
  tension. This is the one choice that makes the work visible to the next
  agent, to reporting, and to anyone reviewing this account later.
- **C)** Partially right at best. A verbal mention to a supervisor is still
  outside Sub — it disappears from memory the same way the WhatsApp thread
  will, and it does nothing for the next agent who opens this customer's
  account with no ticket history to read.

</details>

### Scenario: The screen that isn't there

You are helping a customer with a billing question, and while looking
around you notice you cannot open the network monitoring screens that
would show you infrastructure status. A teammate mentions that another
agent's shared login can get into that console "if you're ever stuck."

**What do you do?**

**A)** Note it down as something to ask about later, but do not use the
shared login — treat the missing access as a boundary until told otherwise.
**B)** Use the shared login this one time, since it would help you answer
the customer faster and no one would know.
**C)** Assume it is a bug in your account setup and ask IT to grant you the
same access your teammate has.

<details>
<summary>What happens with each choice</summary>

- **A)** Correct, and the best choice. A missing screen is almost always a
  deliberate boundary tied to a different role and a different kind of
  risk, not an oversight — the right move is to ask through the proper
  channel, not to find a way around it, and definitely not to route around
  it quietly.
- **B)** Wrong, and a serious misstep. Using someone else's login to reach a
  screen your role deliberately does not carry is exactly the "find a
  workaround that reaches it anyway" behavior this chapter warns against —
  it defeats the purpose of the access boundary entirely.
- **C)** Wrong premise, though the instinct to ask is fine. Assuming it is a
  bug skips the more likely explanation — that this is a permission
  boundary by design — and asking IT to just grant it does not confirm
  whether the task actually belongs to your role at all.

</details>

## Summary

- Sub is the operator's official record of subscriber, service, and support
  history — if it is not in Sub, it did not officially happen.
- Your daily work centers on Dashboard, Customers, Support, Workqueue,
  Inbox, and Reports.
- Your role's access is a deliberate, specific bundle of permissions, not an
  accident — a missing screen is a boundary to respect, not a bug to route
  around.
- Every action you take in Sub is part of a permanent, auditable record —
  write and act accordingly.
