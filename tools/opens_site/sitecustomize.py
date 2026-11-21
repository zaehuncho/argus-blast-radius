"""Loaded at interpreter start (on PYTHONPATH) during the isolated file-read pass.

Records every file or directory under tests/ that this process opens or lists,
other than Python sources, and appends them to $ARGUS_OPENS_DIR/<test>.<pid>.txt
at exit. Spawned worker processes inherit PYTHONPATH, so they record too.
"""
import atexit
import os
import sys

_dir = os.environ.get("ARGUS_OPENS_DIR")
_n = os.environ.get("ARGUS_TEST_NUM")
_root = os.path.abspath(os.environ.get("ARGUS_ROOT", "/work/sqlglot"))
_tests = os.path.join(_root, "tests") + os.sep

if _dir and _n:
    _seen = set()

    def _record(path):
        try:
            if isinstance(path, int):
                return
            p = os.path.abspath(os.fsdecode(path))
        except Exception:
            return
        if p.startswith(_tests) and not p.endswith((".py", ".pyc")) and "__pycache__" not in p:
            _seen.add(os.path.relpath(p, _root).replace(os.sep, "/"))

    def _hook(event, args):
        if event == "open":
            _record(args[0])
        elif event in ("os.listdir", "os.scandir", "glob.glob") and args and args[0] is not None:
            _record(args[0])

    sys.addaudithook(_hook)

    @atexit.register
    def _dump():
        if _seen:
            with open(os.path.join(_dir, f"{_n}.{os.getpid()}.txt"), "w") as f:
                f.write("\n".join(sorted(_seen)) + "\n")
