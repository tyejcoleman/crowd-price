"""Crowd Price Studio's launcher:  python3 -I web/studio.py [--maker-name Juniper] [--port 8790]

Isolated (`-I`), like Togi's own launcher: the approver passphrase arrives in this process, so nothing in the directory
you stand in and nothing on PYTHONPATH may load into it. This file imports nothing until sys.path is exactly this repo's
src/ and web/ and Togi's src/ (TOGI_SRC, else found from the `togi` launcher on PATH). Started without -I, it restarts
itself with it.
"""
import os
import sys


def _togi_src():
    env = os.environ.get("TOGI_SRC")
    if env:
        return env
    for d in os.environ.get("PATH", "").split(os.pathsep):
        f = os.path.join(d, "togi")
        if os.path.isfile(f):
            src = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(f))), "src")
            if os.path.isfile(os.path.join(src, "togi", "desk.py")):
                return src
    return None


def _main():
    here = os.path.dirname(os.path.realpath(__file__))
    if not sys.flags.isolated:
        os.execv(sys.executable, [sys.executable, "-I", os.path.join(here, "studio.py"), *sys.argv[1:]])
    root = os.path.dirname(here)
    togi = _togi_src()
    sys.path[:0] = [os.path.join(root, "src"), here] + ([togi] if togi else [])
    from crowdweb.studio_main import main

    return main()


sys.exit(_main())
