"""Crowd Price Studio's launcher:  python3 -I web/studio.py [--maker-name Juniper] [--port 8790] [--togi PATH]

Isolated (`-I`), like Togi's own launcher: the approver passphrase arrives in this process, so nothing in the directory
you stand in and nothing on PYTHONPATH may load into it. Started without -I, it restarts itself with it.

Before anything of this repo or of Togi is imported, their directories are added to sys.path AFTER the standard library
(and before site-packages), so a file in them can never stand in for a standard module (a planted `hashlib.py` would
otherwise see the passphrase). Togi's source is the one the `togi` launcher runs (the `--togi` given, else the one on PATH):
that is the Togi the assistant's tools run, and the desk must be the same Togi. TOGI_SRC, if set, is accepted only when it
is that same directory; otherwise it is ignored and the panel says why approving there is off.
"""
import sys

# The three modules that exist only on Windows are closed before anything else is imported: the standard library probes for them
# (`subprocess` and `mimetypes` do `try: import msvcrt, winreg`) and carries on without, so on every other system the probe walks
# all of sys.path, past site-packages to wherever a .pth file put another project's source. A file of that name there would
# run in the process that receives the approver's passphrase. A `None` in sys.modules makes the import fail where it stands.
if sys.platform != "win32":
    sys.modules.update(dict.fromkeys(("winreg", "_winapi", "msvcrt"), None))

import os  # noqa: E402
import re  # noqa: E402


def _launcher(argv):
    """The `togi` the studio's assistant will use: the last --togi given (as argparse reads it), else the one on PATH."""
    given = None
    for i, a in enumerate(argv):
        if a == "--togi" and i + 1 < len(argv):
            given = argv[i + 1]
        elif a.startswith("--togi="):
            given = a.split("=", 1)[1]
    if given is not None:
        return given
    for d in os.environ.get("PATH", "").split(os.pathsep):
        f = os.path.join(d, "togi")
        if d and os.path.isfile(f) and os.access(f, os.X_OK):
            return f
    return None


def effective_src(src):
    """The directory Togi's own `_launch.py` puts on sys.path when run from `src`: the one that holds the real file (it
    follows symlinks, so a `togi` linked in from elsewhere is that elsewhere). None if `src` holds no Togi."""
    f = os.path.join(src, "togi", "_launch.py") if src else ""
    if not (os.path.isabs(src or "") and os.path.isfile(f)):
        return None
    return os.path.dirname(os.path.dirname(os.path.realpath(f)))


def launcher_src(path):
    """The Togi source a `togi` launcher runs, or None when that cannot be told. Togi's own launcher (bin/togi, through any
    symlinks) runs <its checkout>/src; a small wrapper script that names one `<src>/togi/_launch.py` runs that."""
    if not path or not os.path.isfile(path):
        return None
    real = os.path.realpath(path)
    src = effective_src(os.path.join(os.path.dirname(os.path.dirname(real)), "src"))
    if src:
        return src
    try:
        with open(real, "rb") as f:
            text = f.read(8192).decode("utf-8", "replace")
    except OSError:
        return None
    found = set(re.findall(r"""([^\s"'$]+)/togi/_launch\.py""", text))
    return effective_src(found.pop()) if len(found) == 1 else None


def togi_src(argv, env):
    """(the Togi source to import, why not) for this studio."""
    wanted = launcher_src(_launcher(argv))
    given = env.get("TOGI_SRC")
    if given:
        if wanted and effective_src(given) == wanted:
            return wanted, None
        return None, {"code": "togi_src_mismatch",
                      "message": "TOGI_SRC is not the Togi your togi launcher runs" + ("" if wanted else " (that launcher's Togi could not be found)"),
                      "next": "unset TOGI_SRC, or point --togi at the launcher of that Togi"}
    if wanted:
        return wanted, None
    return None, {"code": "no_togi", "message": "Togi's source could not be found from the togi launcher",
                  "next": "put Togi's bin/togi on PATH, or pass --togi"}


def after_stdlib(paths):
    """sys.path with `paths` placed after the standard library and before site-packages."""
    import site

    sp = {os.path.realpath(p) for p in site.getsitepackages()}
    at = next((i for i, p in enumerate(sys.path) if os.path.realpath(p) in sp), len(sys.path))
    sys.path[at:at] = [p for p in paths if p not in sys.path]


def _main():
    here = os.path.dirname(os.path.realpath(__file__))
    if not sys.flags.isolated:
        os.execv(sys.executable, [sys.executable, "-I", os.path.join(here, "studio.py"), *sys.argv[1:]])
    root = os.path.dirname(here)
    togi, why = togi_src(sys.argv[1:], os.environ)
    after_stdlib([os.path.join(root, "src"), here] + ([togi] if togi else []))
    from crowdweb import studio_main

    studio_main.TOGI_SRC, studio_main.TOGI_WHY = togi, why
    return studio_main.main()


if __name__ == "__main__":
    sys.exit(_main())
