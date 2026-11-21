# runner.py

`runner.py` runs a candidate set of sqlglot tests on a patched tree inside the scored container and says which fail, counted the way the organizers labeled: a failure, an error or a timeout, a failed subtest (pytest 9 prints `PASSED` beside `SUBFAILED`), or a test file that cannot be imported.

```python
import runner                                   # /app is on sys.path for /app/selector.py
files = runner.files_from_patch(patch_text)     # {path: new text, or None if deleted}; ValueError if it does not apply
res = runner.run_candidates(files, nodeids, timeout_s=30)
res["failing"]        # set of node ids, or None (timeout, crash, unfinished): fall back to static
res["cpu_s"]          # CPU of pytest and every process it waited for (pool workers included)
res["status"]         # "ok", "timeout", "crash(exit=N)", "unfinished(N)", "exit=N"
res["missing"]        # candidates that no longer exist after the patch (counted as failing)
res["known_failing"], res["passed"], res["unknown"]   # per-test answers, final even when failing is None
```

Keyword options:

| Option | Default | Meaning |
| --- | --- | --- |
| `timeout_s` | 60 | Wall limit for the call. Past it the process group is killed and `failing` is `None`. |
| `per_test_timeout_s` | `timeout_s / 2` | pytest-timeout per test, so one hanging test fails instead of sinking the call. |
| `pool_workers` | 1 | Size of the tests' `ProcessPoolExecutor` (`PYTHON_CPU_COUNT`). 1 is cheapest in CPU; 2 is faster in wall time on the executor tests. |
| `mode` | `"fresh"` | `"fresh"` starts a new interpreter per call. `"fork"` forks a warm server (see below). `ARGUS_RUNNER_MODE` sets the default. |
| `missing_as_failing` | True | A candidate that no longer exists counts as failing. |

`runner.start_fork_servers()` warms a server ahead of time; `runner.stop_fork_servers()` shuts it down. Neither is required.

## What the production Dockerfile needs

After the `FROM` line, before the sqlglot fetch (so the layer caches):

```dockerfile
# The packages the organizers labeled with: pytest 9, pytest-timeout, duckdb, pandas...
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --root-user-action=ignore -r /app/requirements.lock
```

After the sqlglot fetch and before the existing `RUN chmod -R a+rX /app`:

```dockerfile
# Bytecode for sqlglot and its tests, plus pytest's rewritten test modules,
# so a run only compiles what a patch changed.
COPY runner.py /app/runner.py
RUN python /app/runner.py --prepare /app/sqlglot
```

That is all. `.dockerignore` already lets `runner.py` and `requirements.lock` through. The prepare step takes about 3 s (310 `.pyc` files) and fails the build if any test module does not collect. Everything a run writes (the overlay, the pytest plugin, `TMPDIR`) is under `/tmp` and removed after the call. This exact Dockerfile (b708869 plus these lines) was built and run under the Makefile's flags.

## How it works

- **Overlay, not a copy.** Each call builds `/tmp/argus-run-*/repo`, a directory of symlinks into `/app/sqlglot`. Directories are real only along the paths of the patched files, and the patched files are written in place. Fixtures resolve through `tests/helpers.py`'s `FIXTURES_DIR`. `PYTHONPATH=<overlay>` and the overlay as cwd mean the spawned `ProcessPoolExecutor` workers import the patched code too: a synthetic patch that raises only inside a pool worker is caught.
- **`Path(__file__).resolve()`** follows a symlink back to the unpatched checkout. Only `tests/test_sqlglotc.py` does that (to find `sqlglotc/setup.py`), so when a patch touches anything outside `sqlglot/` and `tests/` that file is copied rather than linked.
- **No stale bytecode.** Each real overlay directory gets its own `__pycache__` of symlinks to the prebuilt `.pyc` files, minus the patched modules' (and pytest's rewritten `*-pytest-9.2.0.pyc` for a patched test file). The build's timestamp `.pyc` files are therefore safe without `checked-hash`, and a same-size edit still recompiles.
- **One `python -m pytest` per call**, given the candidate files in pytest's directory order. A small plugin, written to `/tmp` on first use, keeps only the candidate items and records every failed report (setup, call, teardown, each subtest), every collector that failed to import, each finished item and the session end. `--continue-on-collection-errors` keeps other files running. `-p no:cacheprovider`, plugin autoload off, `pytest_timeout` loaded explicitly, xdist not loaded, `--tb=no -q`.
- **Environment as labeled.** `SKIP_INTEGRATION=0`, sqlglot on `PYTHONPATH` (not installed), `TMPDIR` inside the run directory. `PYTHON_CPU_COUNT=<pool_workers>`: without it the optimizer and executor tests start `os.process_cpu_count()` spawned workers, the host's core count, each importing sqlglot, duckdb and pandas. One optimizer test cost 15.7 s of CPU with 16 workers, 2.6 s with 2 and 2.0 s with 1. `OPENBLAS_NUM_THREADS=1` and friends stop numpy from starting a thread per host core in every process (the container allows 256 pids).
- **No zombies.** Each pool-using run leaves multiprocessing's resource tracker behind as an orphan, and in the scored container orphans go to PID 1, the selector. Each would hold one of the 256 pids for the rest of the batch. The runner makes its process a child subreaper and waits for zombie children it did not start itself at the start and end of every call. If the selector ever runs its own subprocesses in another thread while a runner call is in flight, that reaper could collect one of them first; don't do that.
- **Fails closed.** A timeout, a missing session end (a crash or `os._exit`), an unfinished candidate, or an exit code other than 0, 1 or 5 gives `failing=None`. A finished test's answer is still final, in `known_failing` and `passed`.

## Fork server (`mode="fork"`)

A server process imports pytest, numpy, pandas and duckdb once, never sqlglot or the tests. Each call forks it. The child takes the call's cwd, environment, `sys.path` and `sys.argv` exactly as `python -m pytest` would have them, and calls pytest's console entry point. Pool workers are still spawned fresh from the overlay. The server never inherits the selector's stdout. It refuses to fork if it has more than one thread, and any server failure falls back to fresh runs for the rest of the batch.

Evidence that it answers like a fresh process: every sample call and every synthetic edge case gave the same answer in both modes (see below). It saves about 0.1 to 0.2 s of CPU per plain call and 0.4 to 0.7 s per optimizer or executor call, and the server costs 0.3 to 0.8 s once. It pays off only when a batch makes several optimizer or executor calls, which is why `fresh` stays the default.

## Measured (`python tools/test_runner.py`, scorer's `docker run` flags)

The host was shared with another job running up to 10 pytest containers, so CPU figures from the busy periods run up to 1.5 times higher. The figures below come from the quietest runs.

**Accuracy.** No wrong answer anywhere. A wrong "passes" never happened, and the only non-answer was one timeout (`failing=None`) while the host was swapping.

| Check | Calls | Result |
| --- | --- | --- |
| 30 sample commits: true failing tests plus 3 passing tests from each of their files, plus random sets of 1, 5 and 20 tests | 120 fresh (2 pool workers), 240 fresh+fork (1), 274 fresh+fork (1, final code) | all exact, except 1 timeout under host swapping |
| The current selector's own picks for each sample commit (up to 36 recorded s, e.g. 4d63's 1,088 tests, 535z's 171 tests with 1 failing) | 48 fresh+fork | all exact |
| Synthetic edge cases: failure only inside pool workers, broken sqlglot import, broken test-file import, same-size edit, hang (per-test timeout), hang (call timeout), `os._exit`, removed test, deleted test file plus new module, broken `sqlglotc/setup.py` | 22 fresh+fork | all as expected |
| All 1,620 tests on the unpatched tree | 1 | 1,620 passed, 25,440 subtests, 0 failing or missing |

**CPU per call** (`cpu_s`, which includes the pool workers; the container's cgroup counter adds about 0.03 s per call). The executor's `setUpClass` loads TPC-H and TPC-DS, and the recorded seconds leave that out.

| Candidate set | Fixed, fresh | Fixed, fork | Per recorded second |
| --- | --- | --- | --- |
| plain (no `test_optimizer` / `test_executor`) | 0.35 to 0.55 s (one trivial test: 0.33 s) | 0.25 to 0.37 s | about 1.0 |
| any `test_optimizer` test | 1.1 to 1.25 s | 0.5 to 0.8 s | about 1.4 |
| any `test_executor` test | 2.0 to 2.2 s | 1.5 s | 1 to 2 |

Fixed cost breaks down roughly as interpreter 0.01 s, pytest 0.13 s, sqlglot 0.11 s, a dialect test module 0.14 s, and collection. Each spawned pool worker re-imports sqlglot, duckdb and pandas (0.6 s). `pool_workers=1` saved a median 0.34 s per optimizer call against 2, at no cost on plain calls. Wall time is about 0.9 times CPU.

**`/tmp`.** Peak 292 KB in a call (86 candidates); 8 KB stays between calls (the plugin). Nothing leaks: `/dev/shm` is empty and no zombies remain afterwards.

**When it pays.** On the selector's current sample picks the runner beats the static cost only on small plain sets that break nothing: 2qn5 (static 2.54 s, runner 0.9 to 1.2 s), fryt (2.16 s against 0.6 to 0.9 s) and gbav (3.36 s against 2.7 to 3.2 s). Two-test optimizer sets (cost about 2.05 s) need 1 to 2 s of runner CPU and usually do fail, and executor or large sets cost more CPU than they would save. A workable rule: call it when every candidate is plain and their recorded seconds add up to 1.5 s or less. Above that, compare `0.4 + 1.0 x recorded` with the busiest worker plus 2 s per worker.
