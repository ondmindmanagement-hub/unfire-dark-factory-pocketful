# Unfire Dark Factory — Pocketful

This repository is the result of an Unfire software-factory run built around a Band room with distinct Architect, Builder and Auditor seats.

## Track

Pocketful, completed cumulatively through Stage 4.

Each stage directory is standalone and buildable:

- `stage-1/` — core wallet and payment service
- `stage-2/` — authorizations, holds, captures and browser UI
- `stage-3/` — temporal history, corrections and statement snapshots
- `stage-4/` — refunds and atomic correction batches

The service uses integer minor-unit arithmetic, in-memory state, explicit export/import controls and no live financial credentials or external payment services.

## Repository guide

- `FACTORY.md` — factory roles, handoff protocol, verification gates, failure recovery and measured run data
- `mandates/` — generic mandates for every seat represented in `room.json`
- `room.json` — full Band-room message history exported from the Band API
- `stage-N/` — complete implementation for that stage

## Run a stage

From the repository root:

```sh
docker build -t pocketful-stage-4 ./stage-4
docker run --rm --cpus=2 --memory=2g -e PORT=8080 -p 127.0.0.1:8080:8080 pocketful-stage-4
curl http://127.0.0.1:8080/health
```

Expected health response:

```json
{"status":"ok"}
```

Each stage also includes `RUN.md` and builder tests.

## Verification

Final Stage 4 verification completed successfully against the shipped challenge harness.

Host mode:

- Stage 1: 147/147
- Stage 2: 35/35
- Stage 3: 6/6
- Stage 4: 5/5
- highest contiguous stage: 4
- claimed stage: 4

Isolated mode:

- Stage 1: PASS
- Stage 2: PASS
- Stage 3: PASS
- Stage 4: PASS
- highest contiguous stage: 4
- claimed stage: 4

An additional Stage 4 adversarial script passed refund replay, refund limits, immutability, batch replay, settlement completeness, atomicity and concurrent stale-revision checks.

To reproduce with the official challenge package:

```sh
python -m harness run --track pocketful --repo /absolute/path/to/this-repo --stage 4 --mode isolated
python -m harness check /absolute/path/to/this-repo --track pocketful
```

## Team

Unfire — Omar Baró.

This is a specification exercise for the hackathon and not a production financial service.
