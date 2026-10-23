# Airport Customer Integration Architecture

An independent system-design case study. All customers, airlines and data are synthetic.

## Overview

Airports and ground handlers run on data from systems that don't agree with each other. Flight APIs and baggage exports each use their own identifiers, time formats and file formats. This project shows how to bring those sources into one reliable model, so a new customer can be added without rebuilding the system.

## Architecture

![Integration architecture](architecture.svg)

Customer data passes through **adapters** into a shared **canonical model**, goes through **validation**, and only trusted records reach the **dispatch platform**.

## Key Design Decisions

- **Reusable adapters:** each customer's format is handled in one small adapter, so onboarding is repeatable and customer formats never reach the core.
- **Shared validation:** the same rules apply to every customer, so unreliable data can't slip through to downstream systems. An identical duplicate is accepted once. Conflicting records for the same flight are held for review, never silently overwritten.
- **Three explicit outcomes:** every record is **accepted**, **held for review** or **rejected** with a reason, so problems are visible rather than silently ignored.

## Customer Onboarding

**Discover → Design → Validate → Deploy**

Adding the second fictional customer took one new adapter and its tests. The core model and validation logic didn't change.

## Run the Demo

Requires Python 3.9+ and only the standard library.

```bash
python3 integration.py
python3 -m unittest -v
```

The demo shows the same flight, sent in two customers' different formats, producing an identical record. Bad or conflicting data is rejected or held for review instead of being guessed. The 15 sample records give 7 accepted, 2 held for review and 6 rejected.

*This is a prototype. A production system would also need persistence, deduplication and update ordering across runs, monitoring and failure recovery.*
