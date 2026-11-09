---
chapter: 3
title: "Reading a Customer: Status, Service, and Bandwidth"
part: "The Work"
---

# Reading a Customer: Status, Service, and Bandwidth

"Is this customer active?" sounds like a one-field question. It is not.
Sub tracks a customer's **account status**, their **service/subscription
status**, and their **connection state** as three separate things that can
each say something different at the same moment — and misreading which one
answers the question you were actually asked is one of the most common
ways a support call goes sideways. This chapter teaches you to read a
customer record correctly.

**By the end of this chapter you can:**

- Tell the difference between account status and service status, and know
  which one answers which question.
- Read a customer's plan and bandwidth correctly.
- Explain why a customer can be "active" and still show "Not connected" —
  or vice versa — and tell that apart from a merely stale "Last seen"
  reading.
- Navigate a customer record without needing to ask someone else first.

FIGURE `SUP-03-02` *The Customers directory: search, filter, and open an account.*

## Two different status fields, on purpose

The **account** (the subscriber relationship itself) has its own status,
independent of any one service: `new`, `active`, `blocked`, `suspended`,
`disabled`, `canceled`, `delinquent`. This is the "is this person a
customer in good standing" answer.

Each individual **subscription** (the specific service they are paying
for) has its *own*, separately-tracked status: `pending`, `active`,
`blocked`, `suspended`, `stopped`, `disabled`, `hidden`, `archived`,
`canceled`, `expired`.

These are not the same field shown twice. A customer's account can be
`active` while one specific subscription on that account is `suspended` —
for instance, a business customer with two lines where only one has an
overdue balance. Always check the level the customer is actually asking
about: "is my account okay" is an account-status question; "why is my
internet not working" is almost always a subscription- or
connection-level question.

## Plan and bandwidth

FIGURE `SUP-03-01` *A customer's account overview: status, plan, balance, and the header's Online/Offline connection badge.*

A customer's plan (their `CatalogOffer`) is where the bandwidth numbers
live — download and upload speed — bundled with the price. When a customer
asks "what speed am I paying for," you are reading the plan, not the
account status. When they ask "why is it slow," bandwidth-as-sold is your
starting reference point, not proof of what they are actually getting
right now — that is a network diagnostic question, which is out of scope
for this course and belongs with the technical/network team.

## Connection: a third, live-er signal — at two levels of detail

The customer's own header — visible no matter which tab you're on,
including Account — carries a simple **Connection** stat: **Online** or
**Offline**. This reflects whether the account currently has an active
network session, not whether it is entitled to one. An account can be
fully `active`, on a fully `active` subscription, and still show
**Offline** — the customer's equipment is powered off, or they simply are
not connected right now. Offline is not, by itself, evidence of a fault;
it becomes relevant when the customer says they *expect* to be connected
and are not.

The **Service** and **Network** tabs go one level deeper, per subscription,
with a more granular reading that is not the same simple pair: **Connected**,
**Last seen**, and **Not connected**.

- **Connected** — there is an active session right now.
- **Not connected** — a clean, current reading that there is no active
  session (this is what the header's "Offline" collapses down to).
- **Last seen** — the account's last known session data is stale or
  uncertain. This is a genuinely different reading from "Not connected": it
  means Sub cannot currently confirm the live state one way or the other,
  not that it has confirmed the connection is down. Treating "Last seen" as
  the same thing as "Not connected" is exactly the kind of mix-up that can
  mislead a real "why is it slow" or "is my internet down" call — the header
  badge alone cannot tell the two apart, which is exactly why the
  per-subscription tabs matter once you need that distinction.

Neither "Offline"/"Not connected" nor "Last seen" is, by itself, evidence of
a fault. All of these become relevant when the customer says they *expect*
to be connected and are not — and knowing which reading you are looking at
changes how confidently you can state what is actually happening.

Put the three together in that order — account status, subscription
status, connection state — and you can answer almost any "what's going on
with this customer" question precisely, instead of guessing from one
number.

## Finding the account

Use **Customers → search** by name, email, phone, account number, or
PPPoE identifier — whichever the customer gives you first. Once open, the
account page is organized into tabs: **Account** (the overview you have
just learned to read), **Service**, **Network**, **Billing**, **Tickets**,
**Timeline**. You do not need to master every tab in this course — knowing
that Tickets and Timeline exist on the customer record itself, not only
inside Support, is often the fastest way to see everything that has ever
happened to this customer in one place.

## Common failure modes

- **Reading "active" and stopping there.** Active *what* — the account, or
  the specific service the customer is asking about? They can disagree.
- **Treating "Not connected" as a fault report.** It is a live state, not a
  verdict. Ask what the customer expects before treating it as a problem.
- **Treating "Last seen" as the same thing as "Not connected."** One is a
  stale, uncertain reading; the other is a clean, current one — conflating
  them can mislead a real troubleshooting call.
- **Quoting the plan's bandwidth as a guarantee of current speed.** It is
  what they are sold, not a live measurement.
- **Guessing instead of opening the account.** Every one of these questions
  has a real answer on screen — look before you speculate.

## Do this at work

1. **Open five different customer accounts** and, for each, state out loud:
   account status, one subscription's status, and the connection
   indicator. Notice how often they do not all say the same thing.
2. **Practice restating a customer's question** in terms of which of the
   three levels it is actually about, before you look anything up.
3. **Find a customer whose account and subscription status differ**, if one
   exists in your queue, and understand why.
4. **Locate the Tickets and Timeline tabs** on a customer record you already
   know, and compare what you see there with what Support search shows for
   the same customer.

## Practice scenarios

Illustrative situations, not real customer records.

### Scenario: "Active" but not working

A customer calls: "My account is active — I can see that in the app — but
the internet doesn't work." You have their account open in Sub and need to
decide where to look first.

**What do you do?**

**A)** Trust what the customer read in the app, tell them the account looks
fine on your end too, and suggest they restart their router.
**B)** Check the specific subscription's status before anything else, since
what the app calls "active" is often the account-level status only.
**C)** Check the connection indicator on the Service or Network tab first,
since that most directly answers "the internet doesn't work."

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. This treats the account-level "active" the customer saw as
  the whole answer, when account status and subscription status are
  separate fields that can disagree — an active account with a suspended
  subscription is exactly this situation, and a router restart won't fix a
  billing-driven suspension.
- **B)** Correct, and the best choice. The app the customer sees often
  shows account-level status, which can be `active` while the relevant
  subscription is `suspended` for non-payment. Checking the subscription
  first answers the real question before you waste time elsewhere.
- **C)** Partially right in sequence but wrong as a starting point.
  Connection state is real but it is a live signal about *entitled*
  service — checking it before confirming the subscription is actually
  active risks reading "Not connected" as a network fault when it is really
  an entitlement problem underneath.

</details>

### Scenario: The overdue balance, not connected

A customer with a known overdue balance calls, annoyed that their
connection shows "Not connected." They want to know what's wrong with the
network.

**What do you do?**

**A)** Apologize for the network issue and offer to escalate to the
technical team to investigate the outage.
**B)** Check whether the subscription has moved to a payment-related status
such as `blocked`, `suspended`, or `delinquent`, and explain the real cause
if so.
**C)** Tell the customer their internet is off because they haven't paid,
without checking the account first.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong, and it wastes the technical team's time. Escalating this as
  a network fault skips the much more likely, already-known explanation —
  an overdue balance that has moved the subscription into a
  payment-related status — and sends the ticket to the wrong team entirely.
- **B)** Correct, and the best choice. If the subscription shows a
  payment-related status, the "Not connected" reading is a direct,
  correctly-working consequence of the billing state rather than a fault.
  The honest, useful answer to the customer is about resolving the
  balance, not "let me check the network."
- **C)** Wrong process even if the guess turns out right. Stating the cause
  without confirming it in the account risks being wrong (the subscription
  might, in fact, be unrelated to the balance) and skips the actual check
  this chapter teaches — read the record, don't assume it from the
  customer's billing reputation alone.

</details>

## Summary

- Account status, subscription status, and connection state are three
  separate signals — read all three before answering a "what's going on"
  question.
- Plan and bandwidth live on the subscription's offer; they describe what
  was sold, not a live measurement.
- The customer's header shows a simple **Online/Offline** connection badge
  on every tab; the Service and Network tabs go deeper with **Connected**,
  **Last seen**, and **Not connected**. Neither the header's "Offline" nor
  a stale "Last seen" reading is automatically a fault — check what the
  customer expects before treating either as one, and use the deeper
  per-subscription reading when the header's simple pair isn't enough.
- Customer search takes name, email, phone, account number, or PPPoE ID;
  the account record's own Tickets and Timeline tabs are often the fastest
  way to see full history in one place.
