# Team Argus — Blast Radius

Builders Buildathon 2026, RWX track. Given a patch to [sqlglot](https://github.com/tobymao/sqlglot) at the pinned commit `eb11aec4`, pick the tests that can catch it and pack them onto up to 8 workers. The starter kit's original README is in [KIT_README.md](KIT_README.md).

## How it works

`selector.py` makes a static decision for every changed file:

| Change | Tests selected |
| --- | --- |
| Python source whose code tree is unchanged (comments, blank lines) | none |
| A statement inside a function, method or lambda body | the tests whose recorded coverage executes that statement |
| Module- or class-level code (runs at import) | every test whose process loads that module |
| A test file | the changed tests, or the whole file for module-level edits |
| A data file under `tests/` | the tests that opened it |
| A patched file that no longer parses, renames, added or deleted files, anything unknown | every test |

Class-level edits are narrowed with a class-usage map (`classmap.py`): the tests that read the changed attribute or entered the changed lambda.

After the static pass, two checks run inside the container when the cost model says they pay for their CPU:

- **Run to confirm** (`runner.py`, [RUNNER.md](RUNNER.md)): run a cheap candidate set on the patched tree and keep only the tests that fail.
- **TPC-DS check** (`tpcds_check.py`): `test_execute_tpcds` (21.5 s) optimizes, plans, executes and compares 57 TPC-DS queries. When line coverage alone selects it and the changed statements run nowhere in that test except the parse/optimize step (`map/tpcds_block.json`), the check re-optimizes the queries on the patched tree (about 2.5 s) and compares every optimized tree with the pinned commit's (types, parent links and shared nodes included). Identical trees feed identical later steps that run unchanged code, so the test passes and is dropped. When only a few queries' trees differ (up to 6), the runner runs `test_execute_tpcds` itself narrowed to those queries (`ARGUS_TPCDS_ONLY`, through the test's own setUpClass and process pool): every other query provably behaves as before, so that run decides the whole test. Anything else keeps it.

Then it packs the chosen tests onto workers: longest first onto the least-loaded worker, trying 1 to 8 workers and keeping the cheapest (busiest worker + 2 s per worker).

## The maps

All maps are built from the pinned commit only. There are no answers worked out ahead of time and no table from a patch to its tests.

- `tools/build_coverage.sh` runs sqlglot's suite once with per-test coverage contexts (pytest-cov) and records which files under `tests/` each test opens (`tools/open_tracker.py`).
- `tools/build_iso_coverage.sh` runs every test alone in a fresh process, following the spawned worker processes that sqlglot's optimizer and executor tests use. Shared caches and lazy imports credit code only to the first test that touches it in a single run; isolation fixes that.
- `tools/make_map.py` merges both into `map/cov.sqlite`. It deduplicates line bitmaps, so the file stays a few MB.
- `tools/build_tpcds_block.sh` and `tools/make_tpcds_block.py` build `map/tpcds_block.json`. For each TPC-DS query, a fresh process runs everything `test_execute_tpcds` does after the optimize step, with optimize stubbed to return the pinned commit's tree, under coverage. The image build fingerprints the pinned commit's optimized trees (`tpcds_check.py --build`).
- Validation (local only): `tools/validate_tpcds_check.py` compares the check with full runs of the test on mutants (`tools/gen_tpcds_mutants.py`).

## Run it

```
make sample                      # build the image, run the 30 sample commits, score them
python3 tools/eval.py            # faster loop without Docker: per-commit cost vs. a perfect selection
```

## Open-source code used

- [sqlglot](https://github.com/tobymao/sqlglot) (MIT), fetched at the pinned commit during the image build.
- [coverage.py](https://github.com/nedbat/coveragepy) (Apache-2.0) and [pytest-cov](https://github.com/pytest-dev/pytest-cov) (MIT), used only to build the maps.
- The Blast Radius starter kit (`score.py`, `Makefile`, sample data) from Builders/RWX.

## License

MIT, see [LICENSE](LICENSE).
