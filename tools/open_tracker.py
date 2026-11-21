"""pytest plugin: records which files under tests/ each test opens or lists.

Used only when building maps (tools/build_coverage.sh). Opens made while a test
module is being collected are recorded under "collect:<module nodeid>" and
apply to every test in that module.
"""
import json
import os
import sys

ROOT = os.path.abspath(os.environ.get("ARGUS_ROOT", os.getcwd()))
TESTS = os.path.join(ROOT, "tests") + os.sep
OUT = os.environ.get("ARGUS_OPENS_OUT", "/work/opens.json")

_state = {"current": "<session>"}
_seen = {}


def _record(path):
    try:
        if isinstance(path, int):
            return
        p = os.path.abspath(os.fsdecode(path))
    except Exception:
        return
    if not p.startswith(TESTS) or p.endswith((".py", ".pyc")) or "__pycache__" in p:
        return
    _seen.setdefault(_state["current"], set()).add(os.path.relpath(p, ROOT).replace(os.sep, "/"))


def _hook(event, args):
    if event == "open":
        _record(args[0])
    elif event in ("os.listdir", "os.scandir", "glob.glob"):
        if args and args[0] is not None:
            _record(args[0])


sys.addaudithook(_hook)


def pytest_collectstart(collector):
    _state["current"] = "collect:" + collector.nodeid


def pytest_runtest_logstart(nodeid, location):
    _state["current"] = nodeid


def pytest_sessionfinish(session, exitstatus):
    with open(OUT, "w") as f:
        json.dump({k: sorted(v) for k, v in _seen.items()}, f, indent=0, sort_keys=True)
