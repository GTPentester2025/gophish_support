"""Cross-platform environment bootstrap.

Goal: `python main.py` (or `python start.py`) works on any machine, even when a
stale `venv/` from another computer was shipped alongside the code.

Strategy (in order):
  1. If the required packages already import in the current interpreter, run as-is.
  2. Otherwise build/repair a project venv, install deps, and re-launch into it.
  3. If a venv cannot be created/used on this system, install the deps into the
     current interpreter (with PEP 668 fallbacks) and run without a venv.

A venv is only considered usable if its Python actually *executes* here. A venv
copied from another machine fails this check and is rebuilt automatically.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys

# Import names (not pip names) of the packages start.py / the app need.
REQUIRED_MODULES = ("flask", "requests", "urllib3")


def script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def venv_dir() -> str:
    return os.path.join(script_dir(), "venv")


def venv_python() -> str:
    if os.name == "nt":
        return os.path.join(venv_dir(), "Scripts", "python.exe")
    return os.path.join(venv_dir(), "bin", "python")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _read_pyvenv_cfg() -> dict:
    cfg_path = os.path.join(venv_dir(), "pyvenv.cfg")
    if not os.path.isfile(cfg_path):
        return {}
    result: dict = {}
    with open(cfg_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    return result


def _venv_base_python_missing() -> bool:
    """Heuristic only (diagnostics): cfg points at a base Python path not present.

    Not used as the usability gate because Windows Store Python reports paths
    that are not visible via os.path.isfile even when the venv works.
    """
    cfg = _read_pyvenv_cfg()
    for key in ("home", "executable"):
        path = cfg.get(key, "")
        if path and not os.path.isfile(path):
            return True
    return False


def _python_runs(python_exe: str) -> bool:
    """Authoritative check: does this interpreter actually start and run code?"""
    if not os.path.isfile(python_exe):
        return False
    try:
        subprocess.run(
            [python_exe, "-c", "import sys"],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _venv_usable() -> bool:
    """A venv is usable iff its Python binary actually executes on this machine."""
    return _python_runs(venv_python())


def running_in_project_venv() -> bool:
    return _norm(sys.executable) == _norm(venv_python())


def _in_any_venv() -> bool:
    return getattr(sys, "base_prefix", sys.prefix) != sys.prefix


def deps_available() -> bool:
    """True if every required module can be located in the current interpreter."""
    try:
        return all(importlib.util.find_spec(m) is not None for m in REQUIRED_MODULES)
    except (ImportError, ValueError):
        return False


def _deps_available_in(python_exe: str) -> bool:
    code = (
        "import importlib.util,sys;"
        "m=%r;"
        "sys.exit(0 if all(importlib.util.find_spec(x) is not None for x in m) else 1)"
        % (REQUIRED_MODULES,)
    )
    try:
        return (
            subprocess.run(
                [python_exe, "-c", code], capture_output=True, timeout=60
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _req_path(requirements: str) -> str:
    return os.path.join(script_dir(), requirements)


# Hosts pip contacts for PyPI. Marking them trusted lets installs succeed behind
# corporate SSL-inspection proxies where the company root CA is not in pip's
# certificate bundle (the "unable to get local issuer certificate" error).
TRUSTED_HOSTS = ("pypi.org", "files.pythonhosted.org", "pypi.python.org")


def _trusted_host_flags() -> list:
    flags: list = []
    for host in TRUSTED_HOSTS:
        flags += ["--trusted-host", host]
    return flags


def _pip_attempts(python_exe: str, req_path: str, *, in_venv: bool) -> list:
    """Ordered pip commands to try.

    Covers two independent problems:
      * PEP 668 (externally managed) -> --user / --break-system-packages
        (only meaningful outside a venv)
      * corporate SSL interception -> --trusted-host fallbacks
    """
    install = [python_exe, "-m", "pip", "install"]
    priv_variants = [[]] if in_venv else [[], ["--user"], ["--break-system-packages"]]
    ssl_variants = [[], _trusted_host_flags()]

    attempts: list = []
    for ssl in ssl_variants:
        for priv in priv_variants:
            attempts.append(install + priv + ssl + ["-r", req_path])
    return attempts


def pip_install_into(
    python_exe: str, requirements: str = "requirements.txt", *, in_venv: bool
) -> None:
    req_path = _req_path(requirements)
    if not os.path.isfile(req_path):
        return
    print("[*] Installing Python packages...")
    # Best-effort pip upgrade; never fatal.
    try:
        subprocess.run(
            [python_exe, "-m", "pip", "install", "--upgrade", "pip", *_trusted_host_flags()],
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    last_output = ""
    used_trusted_host = False
    for cmd in _pip_attempts(python_exe, req_path, in_venv=in_venv):
        if "--trusted-host" in cmd and not used_trusted_host:
            print("[*] Standard install failed; retrying via trusted hosts "
                  "(corporate SSL proxy)...")
            used_trusted_host = True
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return
        last_output = (result.stderr or result.stdout or "").strip()
    raise RuntimeError(
        "Failed to install dependencies.\n"
        f"Last error:\n{last_output[-1500:]}\n\n"
        "This usually means no internet access, or a corporate proxy is "
        "blocking PyPI. Options:\n"
        "  * Connect to a network that can reach pypi.org and run: python main.py\n"
        "  * If your company uses a proxy, set it first, e.g. (PowerShell):\n"
        "      $env:HTTPS_PROXY = 'http://user:pass@proxy:port'\n"
        "      python main.py\n"
        "  * Or ask IT for an internal PyPI mirror and set:\n"
        "      $env:PIP_INDEX_URL = 'https://your-mirror/simple'\n"
        "      python main.py"
    )


def ensure_usable_venv() -> str | None:
    """Return a path to a working venv Python, creating/repairing as needed.

    Returns None if a venv cannot be built or run on this system.
    """
    if os.path.isdir(venv_dir()) and not _venv_usable():
        print("[*] Existing virtual environment is not usable here; rebuilding...")
        shutil.rmtree(venv_dir(), ignore_errors=True)

    if not _venv_usable():
        print("[*] Creating virtual environment...")
        try:
            subprocess.check_call([sys.executable, "-m", "venv", venv_dir()])
        except (OSError, subprocess.SubprocessError):
            return None

    return venv_python() if _venv_usable() else None


def _reexec(python_exe: str) -> None:
    """Re-launch the current script with another interpreter (Windows-safe)."""
    script = os.path.abspath(sys.argv[0])
    print("[*] Re-launching with virtual environment Python...")
    rc = subprocess.call([python_exe, script, *sys.argv[1:]])
    sys.exit(rc)


def bootstrap(requirements: str = "requirements.txt") -> None:
    # Already inside the project venv: just make sure deps are present.
    if running_in_project_venv():
        if not deps_available():
            pip_install_into(sys.executable, requirements, in_venv=True)
        return

    # Current interpreter already has what we need: no venv required.
    if deps_available():
        return

    # Try to build/repair a venv and hand off to it.
    py = ensure_usable_venv()
    if py and _norm(py) != _norm(sys.executable):
        if not _deps_available_in(py):
            pip_install_into(py, requirements, in_venv=True)
        _reexec(py)
        return  # not reached

    # No usable venv on this system: install into the current interpreter.
    print("[*] Virtual environment unavailable; installing into current Python...")
    pip_install_into(sys.executable, requirements, in_venv=_in_any_venv())
