"""Team Argus - runs candidate sqlglot tests on patched code and reports which fail.

    from runner import run_candidates, files_from_patch
    files = files_from_patch(open(patch).read())          # {path: new text or None}
    res = run_candidates(files, ["tests/test_parser.py::TestParser::test_union"], 60)
    res["failing"]   # set of node ids, or None when the run was inconclusive
    res["cpu_s"]     # CPU seconds of the pytest process and every process it waited for

How it runs:
  * The patched tree is an overlay under /tmp: a directory of symlinks into the
    pinned checkout, real only along the paths of the patched files. A patched
    fixture is found through tests/helpers.py's FIXTURES_DIR, and the spawned
    ProcessPoolExecutor children import the patched modules through PYTHONPATH.
  * Real overlay directories get their own __pycache__ of symlinks to the
    prebuilt bytecode, minus the patched modules', so nothing stale can load.
  * One `python -m pytest` per call, given the candidate files; a small plugin
    keeps only the candidate items, in collection order, and records every
    failed report (setup, call, teardown, each failed subtest, a timeout) and
    every collector that failed to import.
  * Fails closed: a crash, a missing session end or an unfinished candidate
    gives failing=None. A candidate that no longer exists counts as failing.
  * mode="fork" forks a warm interpreter (pytest, numpy, pandas, duckdb
    imported; never sqlglot) instead of starting one; same answers, less CPU.
  * The calling process reaps the orphans pool-using runs leave behind, so the
    container's 256-pid limit does not fill with zombies.

`python runner.py --prepare /app/sqlglot` at image build time compiles the
bytecode and pytest's rewritten test modules into the checkout. See RUNNER.md.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

SRC = os.environ.get("ARGUS_SQLGLOT", "/app/sqlglot")
TMP = os.environ.get("ARGUS_TMP", tempfile.gettempdir())

PLUGIN_NAME = "argus_runner_plugin"
PLUGIN_SRC = r'''
"""Loaded by runner.py with -p: keeps the wanted items and logs outcomes."""
import json
import os

_out = open(os.environ["ARGUS_RESULTS"], "a", buffering=1, encoding="utf-8")


def _w(tag, value):
    _out.write(json.dumps([tag, value]) + "\n")


def pytest_collectreport(report):
    if report.failed:
        _w("C", report.nodeid)


def pytest_collection_modifyitems(session, config, items):
    with open(os.environ["ARGUS_WANT"], encoding="utf-8") as f:
        want = set(json.load(f))
    keep = [it for it in items if it.nodeid in want]
    if len(keep) != len(items):
        config.hook.pytest_deselected(items=[it for it in items if it.nodeid not in want])
        items[:] = keep
    _tpcds_only(keep)
    for it in keep:
        _w("I", it.nodeid)
    _w("COLLECTED", len(keep))


def _tpcds_only(items):
    # ARGUS_TPCDS_ONLY="i,j,...": TestExecutor's setUpClass loads only those TPC-DS
    # fixture pairs (0-based, file order), so test_execute_tpcds parses, executes and
    # compares just them through its own code path (see tpcds_check.py).
    only = os.environ.get("ARGUS_TPCDS_ONLY")
    if only is None:
        return
    keep = {int(x) for x in only.split(",") if x.strip()}
    for it in items:
        cls, mod = getattr(it, "cls", None), getattr(it, "module", None)
        if cls is None or cls.__name__ != "TestExecutor" or not hasattr(mod, "load_sql_fixture_pairs"):
            continue
        orig = mod.load_sql_fixture_pairs
        if not getattr(orig, "_argus", False):
            def narrowed(filename, _orig=orig):
                pairs = list(_orig(filename))
                if filename.replace("\\", "/").endswith("tpc-ds/tpc-ds.sql"):
                    return [p for i, p in enumerate(pairs) if i in keep]
                return pairs
            narrowed._argus = True
            mod.load_sql_fixture_pairs = narrowed
        break


def pytest_runtest_logreport(report):
    if report.failed:
        _w("F", report.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _w("D", nodeid)


def pytest_sessionfinish(session, exitstatus):
    _w("END", int(exitstatus))
'''

# Environment the organizers labeled with, plus thread caps that change no result.
BASE_ENV = {
    "SKIP_INTEGRATION": "0",
    "PYTHONDONTWRITEBYTECODE": None,
    "PYTHONHASHSEED": None,
    "PYTEST_ADDOPTS": None,
    "PYTEST_PLUGINS": None,
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "ARGUS_TPCDS_ONLY": None,  # set per call through extra_env
    # PYTHON_CPU_COUNT is set per run (pool_workers): sqlglot's optimizer and
    # executor tests start ProcessPoolExecutor(spawn) with the default worker
    # count, os.process_cpu_count(), which is the host's, and every worker
    # imports sqlglot, duckdb and pandas anew (about 0.6 s of CPU each).
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
}
PYTEST_ARGS = [
    "-p", "no:cacheprovider",
    "-p", "pytest_timeout",
    "-p", PLUGIN_NAME,
    "--continue-on-collection-errors",
    "-q", "--tb=no", "--no-header", "-o", "console_output_style=classic",
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- patches

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _split_keep(text):
    """Lines with their '\n' (the last one may lack it). Only '\n' separates."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _strip_prefix(p):
    p = p.split("\t")[0]
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_patch(text):
    """[{old, new, hunks: [(old_start, old_len, [(tag, line)])], binary}]."""
    files, cur = [], None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False}
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
        elif cur is None and line.startswith("--- "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False}
            files.append(cur)
            cur["old"] = _strip_prefix(line[4:])
        elif cur is not None and not cur["hunks"] and line.startswith("--- "):
            cur["old"] = _strip_prefix(line[4:])
        elif cur is not None and not cur["hunks"] and line.startswith("+++ "):
            cur["new"] = _strip_prefix(line[4:])
        elif cur is not None and line.startswith("new file mode"):
            cur["old"] = None
        elif cur is not None and line.startswith("deleted file mode"):
            cur["new"] = None
        elif cur is not None and line.startswith("rename from "):
            cur["old"] = line[len("rename from "):]
        elif cur is not None and line.startswith("rename to "):
            cur["new"] = line[len("rename to "):]
        elif cur is not None and (line.startswith("GIT binary patch") or line.startswith("Binary files ")):
            cur["binary"] = True
        elif cur is not None and line.startswith("@@"):
            m = _HUNK.match(line)
            if not m:
                raise ValueError(f"bad hunk header: {line[:80]}")
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body, o, n = [], 0, 0
            i += 1
            while i < len(lines) and (o < old_len or n < new_len or lines[i].startswith("\\")):
                l = lines[i]
                if l.startswith("\\"):
                    if body:  # "\ No newline at end of file" ends the line before it
                        tag, t = body[-1]
                        body[-1] = (tag, t[:-1] if t.endswith("\n") else t)
                    i += 1
                    continue
                tag = l[:1] if l else " "
                if tag not in " -+":
                    raise ValueError(f"bad hunk line: {l[:80]}")
                body.append((tag, l[1:] + "\n"))
                o += tag in " -"
                n += tag in " +"
                i += 1
            if o != old_len or n != new_len:
                raise ValueError("truncated hunk")
            cur["hunks"].append((old_start, old_len, body))
            continue
        i += 1
    return files


def _apply(old_text, hunks, path):
    old = _split_keep(old_text)
    out, pos = [], 0
    for old_start, old_len, body in hunks:
        start = old_start if old_len == 0 else old_start - 1
        block = [t for tag, t in body if tag in " -"]
        if block and not (start >= pos and old[start:start + len(block)] == block):
            # nearest offset where the hunk's old lines all match, as `git apply` does
            found = None
            for d in range(1, len(old) + 1):
                for at in (start + d, start - d):  # git apply looks forward first on a tie
                    if pos <= at <= len(old) - len(block) and old[at:at + len(block)] == block:
                        found = at
                        break
                if found is not None or (start - d < pos and start + d > len(old) - len(block)):
                    break
            if found is None:
                raise ValueError(f"{path}: hunk at {old_start} does not match")
            start = found
        if start < pos or start > len(old):
            raise ValueError(f"{path}: hunk at {old_start} out of order or range")
        out.extend(old[pos:start])
        o = start
        for tag, t in body:
            if tag in " -":
                if o >= len(old) or old[o] != t:
                    raise ValueError(f"{path}: hunk at {old_start} does not match line {o + 1}")
                if tag == " ":
                    out.append(old[o])
                o += 1
            else:
                out.append(t)
        pos = o
    out.extend(old[pos:])
    return "".join(out)


def files_from_patch(text, src=SRC):
    """{repo-relative path: full new text, or None if the patch deletes it}."""
    result = {}
    for f in parse_patch(text):
        old, new = f["old"], f["new"]
        if f["binary"]:
            raise ValueError(f"binary patch for {new or old}")
        if old is None and new is None:
            continue
        if old is not None:
            with open(os.path.join(src, old), encoding="utf-8", newline="") as fh:
                old_text = fh.read()
        else:
            old_text = ""
        if new is None:
            result[old] = None
            continue
        result[new] = _apply(old_text, f["hunks"], new) if f["hunks"] else old_text
        if old is not None and old != new:
            result.setdefault(old, None)
    return result


# ---------------------------------------------------------------- overlay

# Test files that find repository files through Path(__file__).resolve(), which
# follows a symlink back into the unpatched checkout. They are copied instead
# whenever a patch touches something outside sqlglot/ and tests/ (sqlglotc/...).
RESOLVING_TESTS = ("tests/test_sqlglotc.py",)


def build_overlay(root, patched_files, src=SRC):
    """Symlink farm of src at root, with patched_files written in place."""
    patched = {}
    for p, text in patched_files.items():
        p = os.path.normpath(p).replace(os.sep, "/")
        if p.startswith("../") or p.startswith("/") or p in (".", ""):
            raise ValueError(f"path outside the repository: {p}")
        patched[p] = text
    copies = set()
    if any(not p.startswith(("sqlglot/", "tests/")) for p in patched):
        copies = {t for t in RESOLVING_TESTS
                  if t not in patched and os.path.isfile(os.path.join(src, t))}
    real = {""}
    for p in (*patched, *copies):
        parts = p.split("/")
        for k in range(1, len(parts)):
            real.add("/".join(parts[:k]))
    stems = {}
    for p in patched:
        d, name = os.path.split(p)
        stems.setdefault(d, set()).add(name.rsplit(".", 1)[0])
    for d in sorted(real, key=lambda x: (x.count("/"), x)):
        sdir = os.path.join(src, d) if d else src
        odir = os.path.join(root, d) if d else root
        os.makedirs(odir, exist_ok=True)
        try:
            names = os.listdir(sdir) if os.path.isdir(sdir) else []
        except OSError:
            names = []
        for name in names:
            rel = f"{d}/{name}" if d else name
            if rel in real or rel in patched:
                continue
            if name == "__pycache__":
                skip = tuple(s + "." for s in stems.get(d, ()))
                cdir = os.path.join(odir, name)
                os.mkdir(cdir)
                for pyc in os.listdir(os.path.join(sdir, name)):
                    if skip and pyc.startswith(skip):
                        continue
                    os.symlink(os.path.join(sdir, name, pyc), os.path.join(cdir, pyc))
                continue
            if rel in copies:
                shutil.copy2(os.path.join(sdir, name), os.path.join(odir, name))  # keeps mtime: .pyc stays valid
                continue
            os.symlink(os.path.join(sdir, name), os.path.join(odir, name))
    for p, text in patched.items():
        if text is None:
            continue
        dst = os.path.join(root, p)
        if isinstance(text, bytes):
            with open(dst, "wb") as fh:
                fh.write(text)
        else:
            with open(dst, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
    return root


# ---------------------------------------------------------------- running

_plugin_lock = threading.Lock()


def _plugin_dir():
    d = os.path.join(TMP, "argus-runner-plugin")
    path = os.path.join(d, PLUGIN_NAME + ".py")
    with _plugin_lock:
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read() == PLUGIN_SRC:
                    return d
        except OSError:
            pass
        os.makedirs(d, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(PLUGIN_SRC)
        os.replace(tmp, path)
    return d


def _file_order(path):
    # pytest walks directories by sorted entry name; tests.json follows that order.
    return path.split("/")


def _kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _child_env(extra, pool_workers):
    env = dict(os.environ)
    for k, v in BASE_ENV.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    env["PYTHON_CPU_COUNT"] = str(pool_workers)
    env.update(extra)
    return env


# Pids this module waits for itself (a running pytest, a fork server): the
# orphan reaper never touches them.
_active_pids = set()
_active_lock = threading.Lock()
_subreaper = None


def _become_subreaper():
    """Orphans of our runs (multiprocessing's resource tracker outlives pytest)
    come back to this process instead of PID 1, so _reap_orphans can collect
    them. In the scored container the selector is PID 1 and gets them anyway."""
    global _subreaper
    if _subreaper is None:
        try:
            import ctypes

            one, zero = ctypes.c_ulong(1), ctypes.c_ulong(0)
            _subreaper = ctypes.CDLL(None, use_errno=True).prctl(
                ctypes.c_int(36), one, zero, zero, zero) == 0  # PR_SET_CHILD_SUBREAPER
        except Exception:  # noqa: BLE001
            _subreaper = False


def _reap_orphans():
    """Waits for zombie children we did not start directly: each one holds a pid
    of the container's 256 until someone waits for it."""
    me = str(os.getpid())
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for e in entries:
        if not e.isdigit():
            continue
        try:
            with open(f"/proc/{e}/stat") as fh:
                rest = fh.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if rest[0] == "Z" and rest[1] == me:
            pid = int(e)
            with _active_lock:
                if pid in _active_pids:
                    continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


def _wait_rusage(pid):
    while True:
        try:
            _, status, ru = os.wait4(pid, 0)
            return status, ru.ru_utime + ru.ru_stime
        except InterruptedError:
            continue


def _execute_fresh(args, env, cwd, logpath, deadline, prefix=("-m", "pytest")):
    """`python -m pytest args` in a new session. Returns (exitcode, cpu_s, timed_out)."""
    with open(logpath, "wb") as logf, _active_lock:
        proc = subprocess.Popen([sys.executable, *prefix, *args], cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True)
        _active_pids.add(proc.pid)
    timed_out = threading.Event()

    def expire():
        timed_out.set()
        _kill_group(proc.pid)

    timer = threading.Timer(max(0.1, deadline - time.monotonic()), expire)
    timer.daemon = True
    timer.start()
    try:
        status, cpu = _wait_rusage(proc.pid)
    finally:
        timer.cancel()
    with _active_lock:
        _active_pids.discard(proc.pid)
    proc.returncode = os.waitstatus_to_exitcode(status)
    _kill_group(proc.pid)  # anything the run left behind
    return proc.returncode, cpu, timed_out.is_set()


# ---------------------------------------------------------------- fork server
#
# Optional (mode="fork"). A long-lived interpreter imports pytest (and, by
# default, numpy/pandas/duckdb, which sqlglot's optimizer and executor tests
# import) but never sqlglot or tests. Each run forks it; the child takes the
# run's cwd, environment, sys.path and sys.argv exactly as `python -m pytest`
# would have them, then calls pytest's console entry point. ProcessPoolExecutor
# children are still spawned fresh from the patched overlay.

FORK_SERVER_SRC = r'''
import atexit, json, os, sys
_preload = [m for m in os.environ.pop("ARGUS_PRELOAD", "").split(",") if m]
import pytest
for _m in _preload:
    __import__(_m)
_rf = os.fdopen(int(sys.argv[1]), "r", encoding="utf-8")
_wf = os.fdopen(int(sys.argv[2]), "w", encoding="utf-8", buffering=1)
_base = list(sys.path[1:])
_main = os.path.join(os.path.dirname(pytest.__file__), "__main__.py")
try:  # forking is only safe while the server has one thread
    _threads = int([l for l in open("/proc/self/status") if l.startswith("Threads:")][0].split()[1])
except Exception:
    _threads = None
_wf.write(json.dumps({"ready": os.getpid(), "threads": _threads}) + "\n")
for _line in _rf:
    _req = json.loads(_line)
    sys.stdout.flush(); sys.stderr.flush()
    _pid = os.fork()
    if _pid == 0:
        _rc = 3
        try:
            _rf.close(); _wf.close()
            os.setsid()
            os.chdir(_req["cwd"])
            os.environ.clear(); os.environ.update(_req["env"])
            _fd = os.open(_req["log"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.dup2(_fd, 1); os.dup2(_fd, 2); os.close(_fd)
            _fd = os.open(os.devnull, os.O_RDONLY); os.dup2(_fd, 0); os.close(_fd)
            sys.path[:] = [_req["cwd"], *_req["pythonpath"], *_base]
            sys.argv[:] = [_main, *_req["args"]]
            del _line, _req, _fd
            _rc = pytest.console_main()
        except SystemExit as _e:
            _rc = _e.code if isinstance(_e.code, int) else 1
        except BaseException:
            import traceback; traceback.print_exc()
        finally:
            try:
                atexit._run_exitfuncs()
                sys.stdout.flush(); sys.stderr.flush()
            finally:
                os._exit(_rc if isinstance(_rc, int) and 0 <= _rc < 256 else 1)
    _wf.write(json.dumps({"pid": _pid}) + "\n")
    while True:
        try:
            _, _st, _ru = os.wait4(_pid, 0)
            break
        except InterruptedError:
            continue
    _wf.write(json.dumps({"status": _st, "cpu": _ru.ru_utime + _ru.ru_stime}) + "\n")
'''

DEFAULT_PRELOAD = "numpy,pandas,pandas.testing,duckdb"


class ForkServer:
    def __init__(self, pool_workers, preload):
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        env = _child_env({"PYTHONPATH": _plugin_dir(), "ARGUS_PRELOAD": preload}, pool_workers)
        # stdout is the selector's answer channel: the server never inherits it.
        with _active_lock:
            self.proc = subprocess.Popen([sys.executable, "-c", FORK_SERVER_SRC, str(r1), str(w2)],
                                         pass_fds=(r1, w2), env=env, cwd="/",
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            _active_pids.add(self.proc.pid)
        os.close(r1)
        os.close(w2)
        self.w = os.fdopen(w1, "w", encoding="utf-8", buffering=1)
        self.r = os.fdopen(r2, "r", encoding="utf-8")
        msg = self.r.readline()
        if not msg:
            raise RuntimeError("fork server did not start")
        self.threads = json.loads(msg).get("threads")
        if self.threads not in (1, None):
            self.close()
            raise RuntimeError(f"fork server has {self.threads} threads; forking it is unsafe")
        self.pythonpath = [p for p in env["PYTHONPATH"].split(os.pathsep) if p]

    def run(self, args, env, cwd, logpath, deadline):
        req = {"cwd": cwd, "env": env, "args": args, "log": logpath,
               "pythonpath": [p for p in env["PYTHONPATH"].split(os.pathsep) if p][:-len(self.pythonpath)]}
        self.w.write(json.dumps(req) + "\n")
        msg = self.r.readline()
        if not msg:
            raise RuntimeError("fork server died")
        pid = json.loads(msg)["pid"]
        timed_out = threading.Event()

        def expire():
            timed_out.set()
            _kill_group(pid)

        timer = threading.Timer(max(0.1, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        try:
            msg = self.r.readline()
        finally:
            timer.cancel()
        if not msg:
            _kill_group(pid)  # the run must not outlive its server
            raise RuntimeError("fork server died")
        done = json.loads(msg)
        _kill_group(pid)
        return os.waitstatus_to_exitcode(done["status"]), done["cpu"], timed_out.is_set()

    def close(self):
        try:
            self.w.close()
            self.proc.wait(5)
        except Exception:  # noqa: BLE001
            self.proc.kill()
            self.proc.wait()
        with _active_lock:
            _active_pids.discard(self.proc.pid)

    def cpu_s(self):
        """The server's own CPU so far (its imports), read from /proc."""
        try:
            with open(f"/proc/{self.proc.pid}/stat") as fh:
                f = fh.read().rsplit(")", 1)[1].split()
            return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
        except (OSError, IndexError, ValueError):
            return None


_servers = {}  # (pool_workers, preload) -> [idle ForkServer]
_servers_lock = threading.Lock()
_fork_disabled = None  # the reason, once a server failed: later calls run fresh


def _take_server(key):
    with _servers_lock:
        idle = _servers.setdefault(key, [])
        if idle:
            return idle.pop()
    return ForkServer(*key)


def _give_server(key, server):
    with _servers_lock:
        _servers.setdefault(key, []).append(server)


def start_fork_servers(n=1, pool_workers=1, preload=DEFAULT_PRELOAD):
    """Warms n servers now (each costs its imports' CPU once)."""
    key = (pool_workers, preload)
    for s in [ForkServer(*key) for _ in range(n)]:
        _give_server(key, s)


def stop_fork_servers():
    with _servers_lock:
        for idle in _servers.values():
            for s in idle:
                s.close()
        _servers.clear()


def run_candidates(patched_files, nodeids, timeout_s=60.0, per_test_timeout_s=None,
                   src=SRC, keep_dir=False, missing_as_failing=True, pool_workers=1,
                   mode=None, preload=DEFAULT_PRELOAD, extra_env=None):
    """Runs nodeids on src with patched_files applied.

    Returns {failing, cpu_s, wall_s, status, missing, collect_errors, exitcode,
    log_tail, known_failing, passed, unknown}.
    failing is a set of node ids, or None when the run as a whole cannot be
    trusted (timeout, crash, an unfinished candidate); status says which. Even
    then known_failing and passed are final for the tests they hold, and unknown
    is the rest (empty when status is "ok").
    pool_workers caps the tests' ProcessPoolExecutor size (PYTHON_CPU_COUNT).
    mode is "fresh" (a new interpreter, the default) or "fork" (fork server).
    """
    mode = mode or os.environ.get("ARGUS_RUNNER_MODE", "fresh")
    t0 = time.monotonic()
    nodeids = list(dict.fromkeys(nodeids))
    res = {"failing": None, "cpu_s": 0.0, "wall_s": 0.0, "status": "", "missing": set(),
           "collect_errors": [], "exitcode": None, "log_tail": "",
           "known_failing": set(), "passed": set(), "unknown": set(nodeids)}
    if not nodeids:
        res.update(failing=set(), status="empty")
        return res
    _become_subreaper()
    _reap_orphans()  # the previous call's resource tracker usually exits after it returned
    run = tempfile.mkdtemp(prefix="argus-run-", dir=TMP)
    try:
        root = build_overlay(os.path.join(run, "repo"), patched_files, src)
        files, missing = [], set()
        for n in nodeids:
            f = n.split("::", 1)[0]
            if not os.path.isfile(os.path.join(root, f)):
                missing.add(n)
            elif f not in files:
                files.append(f)
        files.sort(key=_file_order)
        res["missing"] = missing
        if not files:
            f = set(missing) if missing_as_failing else set()
            res.update(failing=f, known_failing=set(f), unknown=set(), status="ok")
            return res
        results = os.path.join(run, "results.jsonl")
        want = os.path.join(run, "want.json")
        with open(want, "w", encoding="utf-8") as fh:
            json.dump(nodeids, fh)
        tmpdir = os.path.join(run, "tmp")
        os.mkdir(tmpdir)
        env = _child_env({**(extra_env or {}), "PYTHONPATH": root + os.pathsep + _plugin_dir(),
                          "ARGUS_RESULTS": results, "ARGUS_WANT": want, "TMPDIR": tmpdir}, pool_workers)
        if per_test_timeout_s is None:
            per_test_timeout_s = max(1.0, timeout_s / 2)
        args = [*PYTEST_ARGS, f"--rootdir={root}", f"--timeout={per_test_timeout_s:g}", *files]
        logpath = os.path.join(run, "pytest.log")
        deadline = t0 + timeout_s
        outcome = None
        global _fork_disabled
        if mode == "fork" and not _fork_disabled:
            key = (pool_workers, preload)
            server = None
            try:
                server = _take_server(key)
                outcome = server.run(args, env, root, logpath, deadline)
                _give_server(key, server)
            except (OSError, RuntimeError, ValueError) as e:
                if server is not None:
                    server.close()
                _fork_disabled = str(e)
                log(f"runner: fork server failed ({e}); running fresh from now on")
                open(results, "w").close()
                outcome = None
        if outcome is None:
            outcome = _execute_fresh(args, env, root, logpath, deadline)
        exitcode, cpu, timed_out = outcome
        res["cpu_s"] = cpu
        res["exitcode"] = exitcode
        try:
            with open(logpath, "rb") as fh:
                fh.seek(max(0, os.path.getsize(logpath) - 3000))
                res["log_tail"] = fh.read().decode("utf-8", "replace")
        except OSError:
            pass

        found, failed, done, collect_err, end, collected = set(), set(), set(), [], None, False
        try:
            with open(results, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        tag, val = json.loads(line)
                    except ValueError:
                        continue  # a torn last line from a killed run
                    if tag == "I":
                        found.add(val)
                    elif tag == "F":
                        failed.add(val)
                    elif tag == "D":
                        done.add(val)
                    elif tag == "C":
                        collect_err.append(val)
                    elif tag == "COLLECTED":
                        collected = True
                    elif tag == "END":
                        end = val
        except OSError:
            pass
        res["collect_errors"] = collect_err

        def broken_collector(n):
            for c in collect_err:
                if n == c or n.startswith(c + "::") or (c and n.startswith(c + "/")) or c in ("", "."):
                    return True
            return False

        # Final per-test answers even when the run as a whole is not: a finished
        # test's reports are complete (class and module cleanup errors land on
        # the last test of the class or module, which is then unfinished).
        failing = set(missing) if missing_as_failing else set()
        passed, unfinished = set(), set()
        for n in nodeids:
            if n in missing:
                continue
            if broken_collector(n) or n in failed:
                failing.add(n)
            elif n in done:
                passed.add(n)
            elif n not in found and collected:
                res["missing"].add(n)
                if missing_as_failing:
                    failing.add(n)
            else:
                unfinished.add(n)
        res["known_failing"], res["passed"], res["unknown"] = set(failing), passed, unfinished
        if timed_out:
            res["status"] = "timeout"
        elif end is None:
            res["status"] = f"crash(exit={exitcode})"
        elif unfinished:
            res["status"] = f"unfinished({len(unfinished)})"
        elif exitcode not in (0, 1, 5):
            res["status"] = f"exit={exitcode}"
        else:
            res["status"] = "ok"
            res["failing"] = failing
        return res
    finally:
        _reap_orphans()
        res["wall_s"] = time.monotonic() - t0
        if not keep_dir:
            shutil.rmtree(run, ignore_errors=True)


def run_script(patched_files, argv, timeout_s=30.0, src=SRC, extra_env=None):
    """Runs `python *argv` with the overlay of src plus patched_files as working
    directory and PYTHONPATH, environment as for run_candidates. Returns
    {exitcode, cpu_s, status ("ok" or "timeout"), log_tail}."""
    t0 = time.monotonic()
    res = {"exitcode": None, "cpu_s": 0.0, "status": "", "log_tail": ""}
    _become_subreaper()
    _reap_orphans()
    run = tempfile.mkdtemp(prefix="argus-run-", dir=TMP)
    try:
        root = build_overlay(os.path.join(run, "repo"), patched_files, src)
        tmpdir = os.path.join(run, "tmp")
        os.mkdir(tmpdir)
        env = _child_env({**(extra_env or {}), "PYTHONPATH": root, "TMPDIR": tmpdir}, 1)
        logpath = os.path.join(run, "script.log")
        exitcode, cpu, timed_out = _execute_fresh(list(argv), env, root, logpath, t0 + timeout_s, prefix=())
        res.update(exitcode=exitcode, cpu_s=cpu, status="timeout" if timed_out else "ok")
        try:
            with open(logpath, "rb") as fh:
                fh.seek(max(0, os.path.getsize(logpath) - 2000))
                res["log_tail"] = fh.read().decode("utf-8", "replace")
        except OSError:
            pass
        return res
    finally:
        _reap_orphans()
        shutil.rmtree(run, ignore_errors=True)


# ---------------------------------------------------------------- build time

def prepare(src):
    """Compiles sqlglot's and the tests' bytecode, and pytest's rewritten test modules."""
    import compileall

    workers = min(4, os.cpu_count() or 1)
    ok = True
    for d in ("sqlglot", "tests"):
        ok &= bool(compileall.compile_dir(os.path.join(src, d), quiet=1, workers=workers))
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=src, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    # Collecting imports every test module through pytest's assertion rewriter,
    # which caches the rewritten code beside the regular bytecode.
    r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                        "-p", "no:cacheprovider", "-p", "pytest_timeout", "tests"],
                       cwd=src, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail = r.stdout.decode("utf-8", "replace").strip().splitlines()[-1:]
    log(f"prepare: compileall ok={ok}, collect exit={r.returncode} {tail}")
    if r.returncode != 0 or not ok:
        raise SystemExit(1)


def main(argv):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prepare", metavar="SRC")
    ap.add_argument("--patch", help="unified diff to apply (default: none)")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--mode", choices=["fresh", "fork"], default="fresh")
    ap.add_argument("--workers", type=int, default=1, help="ProcessPoolExecutor size in the tests")
    ap.add_argument("nodeids", nargs="*")
    a = ap.parse_args(argv)
    if a.prepare:
        prepare(a.prepare)
        return
    files = {}
    if a.patch:
        with open(a.patch, encoding="utf-8", newline="") as fh:
            files = files_from_patch(fh.read())
    r = run_candidates(files, a.nodeids, a.timeout, keep_dir=a.keep, mode=a.mode,
                       pool_workers=a.workers)
    stop_fork_servers()
    out = {k: (sorted(v) if isinstance(v, set) else v) for k, v in r.items()}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
