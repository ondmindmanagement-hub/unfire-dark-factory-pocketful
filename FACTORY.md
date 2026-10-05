# Factory

## Purpose

This factory separates architecture, implementation and independent verification. The mandates are deliberately generic: another team can point the same roles at a different software problem without changing their operating rules.

The run used one Band room and one cumulative result repository. The room contains six agent identities because provider limits required continuation seats while preserving the same collaboration record:

- Architect — Claude Code / `claude-opus-5-5`
- Builder — Claude Code / `claude-opus-5-5`
- Auditor — Claude Code / `claude-opus-5-5`
- Architect Codex — Codex / `gpt-6-astra`
- Builder Codex — Codex / `gpt-6-astra`
- Auditor Codex — Codex / `gpt-6-astra`

Room id: `b7ad5b8a-1610-4945-8ad5-77d0966b2769`.

## Operating model

The Architect reads the complete authoritative task and converts it into verifiable invariants, concurrency risks, compatibility requirements and acceptance gates. Complete task context is passed to the Builder and Auditor; product requirements stay in handoffs rather than mandates.

The Builder extends only the current cumulative stage, preserves accepted earlier behavior, writes regression tests and reports exact commands and revisions.

The Auditor derives independent tests from the same authoritative requirements. It exercises invalid inputs, retries, concurrency, upgrades, atomic rejection and prior-stage regressions. Failed work returns to the Builder with a reproducer. The Architect accepts a stage only after evidence is available.

For cumulative stages, the accepted prior folder is copied forward. Earlier folders remain independently buildable and are never rewritten to contain later-stage behavior.

## Verification gates

A stage is accepted only when:

1. builder tests pass;
2. the shipped official harness passes all prior and current stages;
3. isolated Docker verification passes;
4. independent adversarial checks do not reveal an unresolved defect;
5. failure evidence is distinguished from test-infrastructure failures.

The final Stage 4 working tree passed the shipped harness in both host and isolated modes with Stage 1 through Stage 4 all passing and `highest_contiguous=4`.

Shipped-check counts in the final host run:

- Stage 1: 147 / 147
- Stage 2: 35 / 35
- Stage 3: 6 / 6
- Stage 4: 5 / 5

The final isolated run also passed all four stages and claimed Stage 4.

## Failures the factory caught

The review loop materially changed the result.

- Stage 1 review found stale-account races around reset/import. The service was hardened so requests re-resolve the authenticated identity under the state lock instead of producing server errors.
- Stage 2 review found a missing visible label in the capture UI. The repair was independently rechecked.
- Stage 3 required statement snapshots to remain stable across later operations and export/import.
- Stage 4 review covered cumulative refund limits, linked-payment immutability, settlement-wide correction completeness, batch atomicity and concurrent stale-revision behavior.
- Several harness failures were infrastructure failures rather than service failures: disk exhaustion, a stopped Docker engine and a cleared Playwright browser cache. Those were repaired and the affected checks were rerun.

## Implementation tradeoffs

The service uses integer minor units for monetary values and a single re-entrant lock as the atomic boundary for mutable state. This favors correctness and auditable behavior over maximal parallel throughput.

State is in-memory, which is sufficient for the challenge contract. Explicit export/import supports state transfer. The service does not depend on outbound network access or live payment credentials.

Independent review increases model and execution cost, but it caught defects that builder happy-path tests did not.

## Measured run data

The exported Band room spans approximately **11.89 hours** from its first to last stored message.

The runtimes were subscription-backed. They did not expose a reliable per-run dollar accounting figure in the room, so dollar model spend is reported as **unavailable rather than zero**. Provider session/credit limits were encountered and are visible in the room history.

Docker, Python 3.12, the challenge harness and Playwright were used for verification.

## Reuse

To reuse this factory:

1. create at least three distinct Band seats;
2. assign the generic role mandates from `mandates/`;
3. give all seats access to the same result checkout and authoritative task;
4. dispatch the complete task to the Architect;
5. require Builder implementation plus independent Auditor verification;
6. preserve accepted revisions and evidence across cumulative stages;
7. keep credentials outside the checkout;
8. export the complete room and run the offline submission check before publishing.

## Provenance note

The room records the full development and continuation history. Provider limits caused seat continuation, and the repository retains that provenance rather than pretending one runtime performed all work.

The final repository was checked with:

```sh
python -m harness check <repo> --track pocketful
python -m harness run --track pocketful --repo <repo> --stage 4 --mode isolated
```
