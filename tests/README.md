# Test suite

Born 2026-07-31 out of `PROD_REVIEW_AND_TEST_SUITE_2026-07-30.md`. Unit
tests only for now — no network, no DB, no Kafka; everything runs against
fakes (see `tests/papertrade/conftest.py:FakePool`).

## Running

```bash
scripts/test.sh                                        # whole suite
scripts/test.sh tests/papertrade/test_exit_reason.py   # one file
scripts/test.sh tests -k gap_guard -x                  # filtered
```

Tests run **in-container only** (papertrade image: python 3.12 + full dep
surface). The host has no venv and host python is 3.14 — do not run pytest
on the host. `scripts/test.sh` starts a throwaway container (`--rm
--no-deps`), installs `requirements/test.txt` into it, and points every
infra env var at an unresolvable host so an accidental escape to real
services fails loudly.

## Conventions

- **Finding regressions**: every CRITICAL/MAJOR finding from the review is
  a named test (`test_c1_*`, `test_c2_*`, `test_c3_*`, `test_m2_*`,
  `test_m3_*`), marked `xfail(strict=True)` while the bug exists. When a
  fix lands the test starts passing, strict-xfail turns that into a loud
  FAILED — remove the marker in the same commit as the fix. Never delete
  the test.
- **Pinned discrepancies**: where code and comments disagree (e.g.
  orderwin_long stop exemption), the CURRENT behavior is pinned with a
  comment naming the discrepancy, so any change is deliberate.
- Determinism: no wall-clock in assertions where the code accepts an
  injected `now`; no randomness without a visible fixed seed.
- `FakePool` raises on unhandled SQL — if production code grows a new
  query, the fake must be extended consciously, never silently no-op.

## Pre-deploy gate

Run `scripts/test.sh` before any `docker compose up -d --build <svc>`.
This suite exists because prod changes do not pass through CI on this VM.
