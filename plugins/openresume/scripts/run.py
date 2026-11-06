"""Run the bundled OpenResume source using a user-local virtual environment."""

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def _python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _runtime(requirements: Path, environment: dict[str, str]) -> Path:
    identity = (sys.implementation.name, sys.version, sys.base_prefix, platform.machine())
    digest = hashlib.sha256(repr(identity).encode() + requirements.read_bytes()).hexdigest()[:20]
    cache = Path.home() / ".openresume" / "runtime" / digest
    cache.mkdir(parents=True, exist_ok=True)
    for ready in sorted(cache.glob("env-*/.ready")):
        interpreter = _python(ready.parent)
        if interpreter.is_file():
            return interpreter

    # Build in its final location. Other processes only discover complete environments.
    destination = Path(tempfile.mkdtemp(prefix="env-", dir=cache))
    interpreter = _python(destination)
    try:
        venv.EnvBuilder(with_pip=True).create(destination)
        subprocess.run(
            [str(interpreter), "-m", "pip", "--disable-pip-version-check", "--no-input",
             "install", "--no-compile", "--only-binary=:all:", "-r", str(requirements)],
            env=environment, stdout=sys.stderr, check=True,
        )
        subprocess.run(
            [str(interpreter), "-c", "import docx, pymupdf"],
            env=environment, stdout=sys.stderr, check=True,
        )
        (destination / ".ready").touch()
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return interpreter


def main() -> int:
    if sys.version_info < (3, 11):
        print("OpenResume requires Python 3.11 or newer.", file=sys.stderr)
        return 1
    scripts = Path(__file__).resolve().parent
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    environment["PYTHONUTF8"] = "1"
    try:
        interpreter = _runtime(scripts / "requirements.txt", environment)
        return subprocess.run(
            [str(interpreter), str(scripts / "openresume"), *sys.argv[1:]],
            env=environment,
        ).returncode
    except (OSError, subprocess.CalledProcessError) as error:
        print(
            f"OpenResume could not prepare its runtime. Check that Python includes venv and pip, "
            f"and that dependency wheels are available for this platform. {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
