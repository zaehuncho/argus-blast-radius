"""Checks runner.py against the 30 sample commits under the scorer's docker limits.

Host side (default): builds tools/Dockerfile.runner-dev, then runs this script
inside it with the Makefile's RUN_FLAGS, the working-tree runner.py mounted over
the image's copy, and sample/answers.json mounted for grading.

Inside (--inside), for every sample commit:
  (a) accuracy: the true failing tests plus up to 3 passing tests from each of
      their files (or, for a commit that breaks nothing, from the test files
      its changed paths map to); the runner must report exactly the failing ones.
  (b) cost: candidate sets of 1, 5 and 20 tests from those files; CPU seconds,
      wall seconds and /tmp peak per call. Their outcomes are graded too.

Each candidate set runs once per --modes entry (fresh, fork), and the modes'
answers are compared with each other as well as with the truth.

  --edge-only       synthetic changes the samples lack (a failure only in pool
                    workers, broken imports, hang, crash, removed or deleted
                    tests, a same-size edit, sqlglotc/setup.py)
  --full-baseline   all 1620 tests once on the unpatched tree (must all pass)

Usage (Windows or Linux; Docker in WSL is used through `wsl -u root`):
  python tools/test_runner.py [--no-build] [--only ID,ID] [--sizes 1,5,20] [--skip-cost]
                              [--modes fresh,fork] [--workers 1] [--json-out FILE]
Exit status 0 means every call matched and the modes agreed.
"""
import argparse
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

POOL_FILES = {"tests/test_optimizer.py", "tests/test_executor.py"}
CLASSES = ("plain", "optimizer", "executor")


def call_class(nodeids):
    """executor: any test_executor test (its setUpClass loads TPC-H/TPC-DS, which the
    recorded seconds do not include); optimizer: any test_optimizer test; else plain."""
    files = {n.split("::")[0] for n in nodeids}
    if "tests/test_executor.py" in files:
        return "executor"
    return "optimizer" if "tests/test_optimizer.py" in files else "plain"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
IMAGE = "argus-runner-dev"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- host side

def to_linux(path):
    path = os.path.abspath(path)
    if os.name != "nt":
        return path
    drive, rest = os.path.splitdrive(path)
    return f"/mnt/{drive[0].lower()}{rest.replace(os.sep, '/')}"


def sh(cmd, **kw):
    """Runs a shell command line on the Docker host (WSL root on Windows)."""
    if os.name == "nt" and not shutil.which("docker"):
        return subprocess.run(["wsl", "-u", "root", "-e", "bash", "-lc", cmd], **kw)
    return subprocess.run(["bash", "-lc", cmd], **kw)


def run_flags():
    """The Makefile's RUN_FLAGS with $(SAMPLE) filled in."""
    text = open(os.path.join(REPO, "Makefile"), encoding="utf-8").read().replace("\\\n", " ")
    m = re.search(r"^RUN_FLAGS\s*:=\s*(.*)$", text, re.M)
    flags = m.group(1).replace("$(SAMPLE)", to_linux(os.path.join(REPO, "sample")))
    return flags


def host(args, rest):
    lrepo = to_linux(REPO)
    if not args.no_build:
        cpus = f"--cpuset-cpus {args.build_cpus}" if args.build_cpus else ""
        cmd = (f"cd {shlex.quote(lrepo)} && docker build -q --platform linux/amd64 {cpus} "
               f"-f tools/Dockerfile.runner-dev -t {IMAGE} .")
        log("build:", cmd)
        t = time.time()
        r = sh(cmd)
        if r.returncode:
            sys.exit(f"build failed ({r.returncode})")
        log(f"build done in {time.time() - t:.0f}s")
    mounts = (f"--volume {shlex.quote(lrepo)}/runner.py:/app/runner.py:ro "
              f"--volume {shlex.quote(lrepo)}/tools/test_runner.py:/app/tools/test_runner.py:ro "
              f"--volume {shlex.quote(lrepo)}/sample/answers.json:/data/answers.json:ro ")
    if args.selection:
        mounts += f"--volume {shlex.quote(to_linux(args.selection))}:/data/selection.jsonl:ro "
    inner = " ".join(shlex.quote(x) for x in ["python", "/app/tools/test_runner.py", "--inside", *rest])
    cmd = f"docker run {run_flags()} {mounts} {IMAGE} {inner}"
    log("run:", cmd)
    t = time.time()
    r = sh(cmd, stdout=subprocess.PIPE)
    out = r.stdout.decode("utf-8", "replace")
    log(f"container exit={r.returncode} wall={time.time() - t:.1f}s")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(out)
    try:
        summary = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(out)
        sys.exit("no summary from the container")
    print(json.dumps(summary["summary"], indent=1))
    s = summary["summary"]
    sys.exit(0 if s["mismatched_calls"] == 0 and not s["mode_disagreements"] else 1)


# ---------------------------------------------------------------- inside the container

def cgroup_cpu_usec():
    try:
        with open("/sys/fs/cgroup/cpu.stat") as fh:
            for line in fh:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except OSError:
        return None


class TmpWatch:
    """Polls /tmp usage; peak bytes used above the starting point."""

    def __init__(self, path="/tmp", every=0.02):
        self.path, self.every = path, every
        self.base = self.used()
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def used(self):
        st = os.statvfs(self.path)
        return (st.f_blocks - st.f_bfree) * st.f_frsize

    def _loop(self):
        while not self._stop.wait(self.every):
            self.peak = max(self.peak, self.used() - self.base)

    def __enter__(self):
        self.peak = 0
        self._stop.clear()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join()
        self.peak = max(self.peak, self.used() - self.base)


def map_test_files(paths, files):
    """Test files a change most plausibly reaches, by name."""
    out = []
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        cands = []
        if p.startswith("tests/") and p.endswith(".py") and p in files:
            cands.append(p)
        elif p.startswith("tests/fixtures/optimizer/"):
            cands += ["tests/test_optimizer.py", "tests/test_executor.py"]
        elif p.startswith("tests/fixtures/"):
            cands += ["tests/test_transpile.py", "tests/test_parser.py"]
        elif re.match(r"sqlglot/(dialects|parsers|generators|tokenizers|typing)/", p):
            cands += [f"tests/dialects/test_{stem}.py", "tests/dialects/test_dialect.py"]
        elif p.startswith("sqlglot/optimizer/"):
            cands.append("tests/test_optimizer.py")
        elif p.startswith("sqlglot/executor/"):
            cands.append("tests/test_executor.py")
        elif p.startswith("sqlglot/expressions/"):
            cands += ["tests/test_expressions.py", "tests/test_parser.py"]
        elif p.startswith("sqlglot/"):
            cands += [f"tests/test_{stem}.py", "tests/test_transpile.py"]
        out += [c for c in cands if c in files and c not in out]
    return out or ["tests/test_transpile.py"]


def edge_cases(src):
    """Synthetic changes the samples do not exercise: (name, files, nodeids, expected, kwargs).
    expected None means the runner must answer inconclusive."""
    def sub(path, old, new):
        with open(os.path.join(src, path), encoding="utf-8", newline="") as fh:
            text = fh.read()
        assert text.count(old) >= 1, (path, old)
        return {path: text.replace(old, new, 1)}

    D = "tests/dialects/test_dremio.py"
    RS = f"{D}::TestDremio::test_regexp_split"
    TM = f"{D}::TestDremio::test_type_mappings"
    PP = "tests/test_optimizer.py::TestOptimizer::test_pushdown_predicates"
    LP = "tests/test_lineage.py::TestLineage::test_lineage"
    SC = "tests/test_sqlglotc.py::TestSQLGlotC::test_dry_run_preserves_obsolete_extensions"
    top = "    def test_regexp_split(self):\n"
    fn = "def pushdown_predicates(expression: E, dialect: DialectType = None) -> E:\n"
    return [
        ("baseline", {}, [RS, TM, PP], set(), {}),
        # Raises only inside spawned ProcessPoolExecutor workers: proves they import the overlay.
        ("child_only", sub("sqlglot/optimizer/pushdown_predicates.py", fn, fn +
                           "    import multiprocessing as _mp\n"
                           "    if _mp.parent_process() is not None:\n"
                           "        raise RuntimeError('argus: patched code ran in a pool worker')\n"),
         [PP, TM], {PP}, {}),
        ("sqlglot_import_broken", sub("sqlglot/expressions/core.py", "import", "imp0rt"),
         [RS, TM, PP], {RS, TM, PP}, {}),
        ("test_file_import_broken", sub(D, "import", "imp0rt"), [RS, TM, PP], {RS, TM}, {}),
        ("same_size_edit", sub(D, 'regexp_split.text("mode"), "ALL"', 'regexp_split.text("mode"), "ALX"'),
         [RS, TM], {RS}, {}),
        ("hang", sub(D, top, top + "        while True:\n            pass\n"), [RS, TM], {RS},
         {"timeout_s": 40, "per_test_timeout_s": 3}),
        ("hang_overall", sub(D, top, top + "        while True:\n            pass\n"), [RS, TM], None,
         {"timeout_s": 4, "per_test_timeout_s": 30}),
        ("crash", sub(D, top, top + "        import os; os._exit(3)\n"), [RS, TM], None, {}),
        ("removed_test", sub(D, top, "    def _gone_regexp_split(self):\n"), [RS, TM], {RS}, {}),
        ("deleted_file_and_new_module", {D: None, "sqlglot/argus_extra.py": "X = 1\n"},
         [RS, TM, LP], {RS, TM}, {}),
        # tests/test_sqlglotc.py finds sqlglotc/setup.py through Path(__file__).resolve().
        ("sqlglotc_setup_broken", sub("sqlglotc/setup.py", "import", "raise RuntimeError('argus')\nimport"),
         [SC, TM], {SC}, {}),
    ]


def inside_edge(runner, modes, workers):
    recs = []
    for name, files, cand, expect, kw in edge_cases(runner.SRC):
        for mode in modes:
            r = runner.run_candidates(files, cand, mode=mode, pool_workers=workers,
                                      **{"timeout_s": 60, **kw})
            got = r["failing"]
            ok = got == expect
            recs.append({"edge": name, "mode": mode, "ok": ok, "status": r["status"],
                         "expected": sorted(expect) if expect is not None else None,
                         "got": sorted(got) if got is not None else None,
                         "cpu_s": round(r["cpu_s"], 3), "wall_s": round(r["wall_s"], 3),
                         "collect_errors": r["collect_errors"], "missing": sorted(r["missing"]),
                         "known_failing": sorted(r["known_failing"]), "passed": sorted(r["passed"]),
                         "unknown": sorted(r["unknown"])})
            log(f"edge {name:28s} {mode:5s} cpu={r['cpu_s']:6.2f}s wall={r['wall_s']:6.2f}s "
                f"{r['status']:14s} {'OK' if ok else 'MISMATCH expected=%s got=%s' % (expect, got)}")
            if not ok:
                recs[-1]["log_tail"] = r["log_tail"][-1500:]
    return recs


def inside(args):
    sys.path.insert(0, "/app")
    import runner

    if args.full_baseline:
        # Every numbered test on the unpatched tree: anything failing here fails
        # only in our environment (the organizers' baseline passes everything).
        ids = [t["id"] for t in json.load(open("/data/tests.json"))["tests"]]
        r = runner.run_candidates({}, ids, timeout_s=1500, per_test_timeout_s=600,
                                  pool_workers=args.workers, mode=args.modes.split(",")[0])
        summary = {"full_baseline": len(ids), "status": r["status"],
                   "failing": sorted(r["failing"]) if r["failing"] is not None else None,
                   "missing": sorted(r["missing"]), "cpu_s": round(r["cpu_s"], 1),
                   "wall_s": round(r["wall_s"], 1), "log_tail": r["log_tail"][-1500:],
                   "mismatched_calls": 0 if r["failing"] == set() else 1, "mode_disagreements": []}
        log(json.dumps(summary, indent=1))
        print(json.dumps({"summary": summary, "calls": []}))
        return
    if args.edge_only:
        modes = args.modes.split(",")
        recs = inside_edge(runner, modes, args.workers)
        runner.stop_fork_servers()
        bad = [r for r in recs if not r["ok"]]
        summary = {"edge_cases": len(recs), "edge_mismatches": bad, "mismatched_calls": len(bad),
                   "mode_disagreements": []}
        log(json.dumps(summary, indent=1))
        print(json.dumps({"summary": summary, "calls": recs}))
        return

    tests = json.load(open("/data/tests.json"))["tests"]
    answers = json.load(open(args.answers))["commits"]
    by_num = {t["number"]: t for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t)
    ids = sorted(f[:-6] for f in os.listdir("/data/commits") if f.endswith(".patch"))
    if args.only:
        ids = [i for i in ids if i in args.only.split(",")]
    sizes = [int(x) for x in args.sizes.split(",") if x] if not args.skip_cost else []
    selection = {}
    if os.path.exists("/data/selection.jsonl"):
        for line in open("/data/selection.jsonl", encoding="utf-8"):
            if line.strip():
                o = json.loads(line)
                selection[o["commit"]] = sorted({n for w in o["workers"] for n in w})

    modes = args.modes.split(",")
    watch = TmpWatch()
    cg0, t_all = cgroup_cpu_usec(), time.time()
    server_cpu = None
    if "fork" in modes:
        t = time.time()
        runner.start_fork_servers(1, pool_workers=args.workers)
        srv = runner._servers[(args.workers, runner.DEFAULT_PRELOAD)][0]
        server_cpu = srv.cpu_s()
        log(f"fork server up in {time.time() - t:.2f}s, {server_cpu}s CPU")
    calls, commits, disagreements = [], [], []
    for cid in ids:
        rng = random.Random(cid)
        with open(f"/data/commits/{cid}.patch", encoding="utf-8", newline="") as fh:
            files = runner.files_from_patch(fh.read())
        fail_ids = {by_num[n]["id"] for n in answers[cid]["failing"]}
        fail_files = sorted({by_num[n]["file"] for n in answers[cid]["failing"]})
        pool_files = fail_files or map_test_files(sorted(files), by_file)
        cheap = [t["id"] for f in pool_files for t in by_file[f]
                 if t["id"] not in fail_ids and t["seconds"] <= args.max_control_s]
        sets = []
        controls = []
        for f in pool_files:
            c = [t["id"] for t in by_file[f] if t["id"] not in fail_ids and t["seconds"] <= args.max_control_s]
            controls += rng.sample(c, min(3, len(c)))
        if not args.selection_only:
            sets.append(("accuracy", sorted(fail_ids) + controls))
            for k in sizes:
                sets.append((f"n{k}", rng.sample(cheap, min(k, len(cheap)))))
        if cid in selection:
            # What the selector actually picked for this commit, when it is cheap enough.
            sel = [by_num[n]["id"] for n in selection[cid]]
            if 0 < sum(by_num[n]["seconds"] for n in selection[cid]) <= args.max_selection_s:
                sets.append(("selection", sel))
        for kind, cand in sets:
            secs = sum(t["seconds"] for t in tests if t["id"] in set(cand))
            spawns = any(c.split("::")[0] in POOL_FILES for c in cand)
            klass = call_class(cand)
            seen = {}
            for mode in modes:
                with watch:
                    cg_a = cgroup_cpu_usec()
                    r = runner.run_candidates(files, cand, timeout_s=args.timeout, mode=mode,
                                              pool_workers=args.workers)
                    cg_b = cgroup_cpu_usec()
                expect = fail_ids & set(cand)
                got = r["failing"]
                seen[mode] = got
                ok = got is not None and got == expect
                rec = {
                    "commit": cid, "kind": kind, "mode": mode, "pool": spawns, "class": klass, "n": len(cand),
                    "recorded_s": round(secs, 3), "cpu_s": round(r["cpu_s"], 3),
                    "wall_s": round(r["wall_s"], 3),
                    "cgroup_cpu_s": round((cg_b - cg_a) / 1e6, 3) if cg_a is not None else None,
                    "tmp_peak_kb": watch.peak // 1024, "status": r["status"], "ok": ok,
                    "expected": sorted(expect), "got": sorted(got) if got is not None else None,
                    "missing": sorted(r["missing"]), "collect_errors": r["collect_errors"],
                }
                if not ok:
                    rec["log_tail"] = r["log_tail"][-1500:]
                calls.append(rec)
                log(f"{cid} {kind:8s} {mode:5s} {klass:9s} n={len(cand):4d} "
                    f"rec={secs:6.2f}s cpu={r['cpu_s']:6.2f}s wall={r['wall_s']:6.2f}s "
                    f"tmp={watch.peak // 1024:6d}KB {r['status']:10s} "
                    f"{'OK' if ok else 'MISMATCH expected=%s got=%s' % (sorted(expect), got)}")
            if len({json.dumps(sorted(v)) if v is not None else "null" for v in seen.values()}) > 1:
                disagreements.append({"commit": cid, "kind": kind,
                                      **{m: sorted(v) if v is not None else None for m, v in seen.items()}})
        commits.append(cid)
    wall = time.time() - t_all
    cg1 = cgroup_cpu_usec()
    runner.stop_fork_servers()

    acc = [c for c in calls if c["kind"] == "accuracy"]
    summary = {
        "modes": modes,
        "pool_workers": args.workers,
        "commits": len(commits),
        "calls": len(calls),
        "mismatched_calls": sum(not c["ok"] for c in calls),
        "mode_disagreements": disagreements,
        "accuracy_commits_ok": {m: sum(c["ok"] for c in acc if c["mode"] == m) for m in modes},
        "accuracy_commits": len(acc) // len(modes),
        "mismatches": [{k: c[k] for k in ("commit", "kind", "mode", "status", "expected", "got",
                                          "missing", "collect_errors", "log_tail") if k in c}
                       for c in calls if not c["ok"]],
        "wall_s_total": round(wall, 1),
        "container_cgroup_cpu_s": round((cg1 - cg0) / 1e6, 1) if cg0 is not None else None,
        "fork_server_start_cpu_s": server_cpu,
        "tmp_peak_kb_max": max((c["tmp_peak_kb"] for c in calls), default=0),
        "tmp_left_kb": (watch.used() - watch.base) // 1024,
    }
    groups = {}
    for c in calls:
        groups.setdefault(f"{c['mode']}/{c['kind']}", []).append(c)
        groups.setdefault(f"{c['mode']}/{c['class']}", []).append(c)
    summary["per_group"] = {
        k: {"calls": len(v), "mean_n": round(sum(c["n"] for c in v) / len(v), 1),
            "mean_recorded_s": round(sum(c["recorded_s"] for c in v) / len(v), 3),
            "mean_cpu_s": round(sum(c["cpu_s"] for c in v) / len(v), 3),
            "median_cpu_s": sorted(c["cpu_s"] for c in v)[len(v) // 2],
            "min_cpu_s": min(c["cpu_s"] for c in v),
            "max_cpu_s": max(c["cpu_s"] for c in v),
            "mean_wall_s": round(sum(c["wall_s"] for c in v) / len(v), 3)}
        for k, v in sorted(groups.items())}
    # cpu_s ~ fixed + per_test * n + per_recorded_s * recorded_s, per mode and per
    # call class: plain (no process pool), optimizer, executor (heavy setUpClass).
    summary["fit_cpu_s"] = {}
    try:
        import numpy as np

        for m in modes:
            for cls in CLASSES:
                v = [c for c in calls if c["mode"] == m and c["class"] == cls]
                if len(v) < 4:
                    continue
                X = np.array([[1.0, c["n"], c["recorded_s"]] for c in v])
                y = np.array([c["cpu_s"] for c in v])
                coef, *_ = np.linalg.lstsq(X, y, rcond=None)
                summary["fit_cpu_s"][f"{m}/{cls}"] = {
                    "calls": len(v), "fixed": round(float(coef[0]), 3),
                    "per_test": round(float(coef[1]), 4), "per_recorded_s": round(float(coef[2]), 3)}
    except Exception as e:  # noqa: BLE001
        summary["fit_cpu_s"] = str(e)
    log(json.dumps(summary, indent=1))
    print(json.dumps({"summary": summary, "calls": calls}))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inside", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--build-cpus", default="0,1", help="cpuset for docker build ('' for all)")
    ap.add_argument("--json-out", help="host: save the container's full JSON here")
    ap.add_argument("--only", help="comma-separated commit ids")
    ap.add_argument("--sizes", default="1,5,20")
    ap.add_argument("--skip-cost", action="store_true")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--max-control-s", type=float, default=1.0)
    ap.add_argument("--answers", default="/data/answers.json")
    ap.add_argument("--modes", default="fresh", help="comma-separated: fresh, fork")
    ap.add_argument("--workers", type=int, default=1, help="pool_workers for run_candidates")
    ap.add_argument("--edge-only", action="store_true", help="run only the synthetic edge cases")
    ap.add_argument("--selection", help="host: a selector output (JSONL); also runs each commit's "
                    "selected tests when they cost at most --max-selection-s recorded seconds")
    ap.add_argument("--max-selection-s", type=float, default=30.0)
    ap.add_argument("--selection-only", action="store_true", help="skip the accuracy and n* sets")
    ap.add_argument("--full-baseline", action="store_true",
                    help="run all 1620 tests once on the unpatched tree")
    args = ap.parse_args()
    if args.inside:
        inside(args)
    else:
        passthrough = ["--sizes", args.sizes, "--timeout", str(args.timeout),
                       "--max-control-s", str(args.max_control_s), "--modes", args.modes,
                       "--workers", str(args.workers)]
        if args.only:
            passthrough += ["--only", args.only]
        if args.skip_cost:
            passthrough.append("--skip-cost")
        if args.edge_only:
            passthrough.append("--edge-only")
        if args.full_baseline:
            passthrough.append("--full-baseline")
        if args.selection_only:
            passthrough.append("--selection-only")
        passthrough += ["--max-selection-s", str(args.max_selection_s)]
        host(args, passthrough)


if __name__ == "__main__":
    main()
