# Venue Reservation

This context describes one automated attempt to reserve and optionally pay for a PKU venue through the EPE system.

## Language

**Reservation Request**:
The ordered venue, date, time, duration, and space preferences supplied for one run.
_Avoid_: Job, task

**Reservation Window**:
A release or returned-slot time at which a fixed attempt budget becomes available.
_Avoid_: Retry batch, round

**Returned Slot**:
A venue slot released after an earlier reservation was not paid in time.
_Avoid_: Reflow slot

**Reservation Candidate**:
One venue space and one or more consecutive available time slots that satisfy a Reservation Request.
_Avoid_: Option, selection

**Rejected Candidate**:
A Reservation Candidate that EPE reported as already taken during the current Reservation Window.
_Avoid_: Blacklisted slot

**Reservation Campaign**:
The ordered execution of all Reservation Windows for one Reservation Request.
_Avoid_: Main loop, retry loop

**Unpaid Order Recovery**:
The lookup that resolves an uncertain submit result to an existing matching unpaid order.
_Avoid_: Order fallback

## Relationships

- A **Reservation Request** starts exactly one **Reservation Campaign**
- A **Reservation Campaign** contains one or more ordered **Reservation Windows**
- A **Reservation Window** evaluates zero or more **Reservation Candidates**
- A **Rejected Candidate** remains rejected only within its current **Reservation Window**
- **Unpaid Order Recovery** may convert an uncertain submission into a successful reservation result

## Example dialogue

> **Dev:** "Should a Rejected Candidate stay rejected during the 12:11 Returned Slot window?"
> **Domain expert:** "No. Rejections belong to one Reservation Window because that candidate may become available again in a later window."

## Flagged ambiguities

- “reflow” and “returned slot” referred to the same behavior; use **Returned Slot** in code and documentation while retaining `--no-reflow` as a CLI compatibility alias.
