# Airport Customer Integration Architecture

An independent system-design case study. All customers, airlines and data are synthetic.

## Overview

Airport systems don't agree: flight APIs and baggage exports each use their own identifiers, time formats and file formats. This project brings them into one reliable model, so a new customer can be added without rebuilding the system.

## Architecture

![Integration architecture](architecture.svg)

Customer data passes through **adapters** into a shared **canonical model**, goes through **validation**, and only trusted records reach the **dispatch platform**.

## Key Design Decisions

- **Reusable adapters:** each customer's format is handled in one small adapter, so onboarding is repeatable and customer formats never reach the core.
- **Shared validation:** the same rules apply to every customer, so unreliable data can't slip through to downstream systems.
- **Three explicit outcomes:** every record is **accepted**, **held for review** or **rejected** with a reason, so problems are visible rather than silently ignored.

## Customer Onboarding

**Discover → Design → Validate → Deploy**

Adding the second fictional customer took one new adapter, its config and tests. The core model and validation logic didn't change.

## Run the Demo

Requires Python 3.9+ and only the standard library.

```bash
python3 integration.py
python3 -m unittest -v
```

The demo shows one flight, sent in two customers' formats, producing an identical record. Bad or conflicting data is rejected or held for review instead of being guessed. The 15 sample records give 7 accepted, 2 held for review and 6 rejected.

## Production Reliability (design only)

**Prototype:** within one run, an identical duplicate flight is accepted once and conflicting flight records are held for review. Nothing is kept between runs.

**Production:**

- **Idempotency:** store each delivery's source, record ID and content hash, so a retry or resend is skipped, even after a restart.
- **Updates:** apply a record only if its source version or timestamp is newer than the stored flight state. Log and ignore older records and duplicates.
- **Authority:** agree during onboarding which source owns each field, e.g. the airline owns flight status. Equal sources that disagree, or records with unreliable timestamps, go to review.
- **Audit:** keep an append-only log of every record received, and derive current state from it.
