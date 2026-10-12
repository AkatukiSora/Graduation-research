# Historical generation_io acceptance tests

These tests preserve the fixed operator/revision acceptance surface. They are intentionally outside pytest's standard `testpaths = ["tests"]` collection because they read fixed `/opt/data` evidence, the G01 selected-projection fixture, P02 runtime and lock files, P05 reference code, or release receipts. They are not portable package tests and are not run by the CI workflow.

Run them only in the original evidence workspace, after confirming those exact read-only inputs are present:

```sh
cd backend/generation_io
uv run --locked --no-sync python -m pytest -q tools/legacy_acceptance/tests
```

No skip/xfail wrappers were added. These historical tests have not been executed as part of D6; missing or changed fixed evidence should remain a failure. External evidence and saved receipts were not modified or reissued. Because D6 changes package-owned files and moves these tests, existing historical receipts that bind prior file hashes may no longer validate; do not treat this README or portable CI run as renewal of those receipts.

## Move map and portable coverage

| Moved test file (all original test bodies retained) | Why it is legacy-only | Portable synthetic coverage retained in `tests/` |
|---|---|---|
| `test_artifacts.py` | Reads fixed G01 selected-fields/contract, imports P05 code, and verifies a saved G02 schema-1 artifact. | `test_portable_artifacts.py`, `test_kernel.py`, `test_portable_cli.py`, and `test_portable_supervisor.py` exercise portable generate/verify, count and hash reconciliation, publication-after-draft-verification, fresh-output/path checks, receipts, cleanup, candidate schema/order/numerics and error paths using only synthetic inputs. `test_kernel.py` checks frozen expected values locally; it does not import P05. |
| `test_resume.py` | Exercises the schema-2 selected-projection artifact API backed by the fixed G01 projection. | `test_portable_artifacts.py` and `test_portable_cli.py` cover portable resume, source-independent kernel-only resume, parent-manifest binding, tamper detection, fresh-output rejection, and read-only input preservation. |
| `test_source.py` | Reads the fixed G01 selected-projection JSONL and its fixture manifest. | `test_portable_input.py` covers validated CP932/k_2.1/170-column streaming, source-row/raw-token provenance, integrity metadata, source hash/CRC failures, and file/path rejection using `tests/fixtures/synthetic/`. |
| `test_raw_source.py` | Loads its header from the fixed G01 fixture manifest. | `test_portable_input.py::test_portable_parser_rejects_bad_header_rows_and_encoding` covers header mismatch, row width, UID validity/duplicates, version and encoding failures; `test_source_hash_and_crc_mismatches_fail_closed` covers integrity failures. |
| `test_revision2_validator.py` | Reads fixed G02 revision-4 release receipts, P02's venv and locks, and historical acceptance references. | Portable tests cover local file/manifest integrity, candidate artifact tampering, schema constraints and non-overwrite behavior. There is deliberately no portable equivalent for validating old operator approval/release receipts, P02's runtime, or its cross-project registry contract. Historical receipt verification remains outside standard CI. |

`tests/test_normalize.py`, `tests/test_region.py`, `tests/test_supervisor.py`, and the portable synthetic input/artifact/kernel/CLI/supervisor tests remain in the standard suite. In particular, the F1 supervisor tests retain their original production resource limits and 120 ms timing assertions; the test-only resource-root fixture redirects only disk-headroom observations to a temporary directory.
