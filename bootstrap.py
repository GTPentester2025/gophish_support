"""Create venv, re-exec into it, and install requirements (PEP 668 safe)."""

import os
import subprocess
import sys


def script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def venv_dir() -> str:
    return os.path.join(script_dir(), "venv")


def venv_python() -> str:
    if os.name == "nt":
        return os.path.join(venv_dir(), "Scripts", "python.exe")
    return os.path.join(venv_dir(), "bin", "python")


def _venv_usable() -> bool:
    py = venv_python()
    return os.path.isfile(py)


def ensure_venv() -> None:
    py = venv_python()
    if os.path.isdir(venv_dir()) and not _venv_usable():
        print("[*] Removing incomplete virtual environment...")
        import shutil

        shutil.rmtree(venv_dir(), ignore_errors=True)
    if not _venv_usable():
        print("[*] Creating virtual environment...")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir()])
        py = venv_python()
    if _venv_usable() and os.path.normcase(
        os.path.abspath(sys.executable)
    ) != os.path.normcase(os.path.abspath(py)):
        print("[*] Re-launching with venv Python...")
        os.execv(py, [py, *sys.argv])


def pip_install(requirements: str = "requirements.txt") -> None:
    req_path = os.path.join(script_dir(), requirements)
    if not os.path.isfile(req_path):
        return
    print("[*] Installing Python packages...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
    )
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-r", req_path],
    )


def bootstrap(requirements: str = "requirements.txt") -> None:
    ensure_venv()
    pip_install(requirements)
