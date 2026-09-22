"""Local and CI quality gate.

Every step runs even after an earlier one fails, so a single run reports
every problem rather than making you re-run to find the next one. A missing
tool is a printed warning, not a silent pass - "the linter was not installed"
and "the linter found nothing" must not look the same.
"""

import shutil
import subprocess
import sys

STEPS: list[tuple[str, list[str]]] = [
    ("compile", [sys.executable, "-m", "compileall", "-q", "src", "tests", "main.py"]),
    (
        "pytest",
        [
            "pytest",
            "--cov=src/app",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-fail-under=80",
        ],
    ),
    ("ruff", ["ruff", "check", "src", "tests", "main.py", "migrations"]),
    ("ruff-format", ["ruff", "format", "--check", "src", "tests", "main.py"]),
    ("mypy", ["mypy", "src"]),
]


def main() -> int:
    failures: list[str] = []
    skipped: list[str] = []

    for name, command in STEPS:
        executable = command[0]
        if executable != sys.executable and shutil.which(executable) is None:
            print(f"[skip] {name}: {executable} is not on PATH")
            skipped.append(name)
            continue
        print(f"\n[run ] {name}: {' '.join(command)}")
        if subprocess.call(command) != 0:
            failures.append(name)

    print("\n" + "=" * 60)
    if skipped:
        print(f"SKIPPED (not installed): {', '.join(skipped)}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
