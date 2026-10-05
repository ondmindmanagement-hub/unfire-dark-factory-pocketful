# Stage 3 — run

Build and start (no manual setup; the image has no runtime dependencies beyond the
Python standard library):

```sh
docker build -t wallet-stage-3 . && docker run --rm -e PORT=8080 -p 8080:8080 wallet-stage-3
```

Then `curl http://localhost:8080/health` returns `{"status": "ok"}`.

State is in memory; seed it with `POST /_test/reset`.

## Layout

| File | Role |
|---|---|
| `app.py` | HTTP server, routing, auth header, idempotency header, error envelope |
| `wallet.py` | Domain core: single-lock store, money movement, idempotency records, reset/export/import |
| `passwords.py` | scrypt password hashing |
| `tests/test_service.py` | Builder's own integration tests (start the server in-process, stdlib only) |

Run the builder tests without Docker (Python 3.10+):

```sh
python3 tests/test_service.py
```
