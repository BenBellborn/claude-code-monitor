# CCM

## Test run time limit
- Full run (`python -m pytest -q -n auto tests`): 1 minute. Measured 2026-11-13: 13 s (17 s without `-n auto`).
- Filtered run (one test file): 1 minute. Measured 2026-11-13: under 1 s.
