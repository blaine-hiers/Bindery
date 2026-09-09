"""run_all_tests.py — run every test in every app, report one verdict.

    py system/apps/run_all_tests.py
    py system/apps/run_all_tests.py -v        # show each test name
    py system/apps/run_all_tests.py kb        # only apps whose folder matches 'kb'

Exit code is 0 only if everything passed, so this is usable in a hook or CI.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
)


TESTS = ROOT / "tests"


def test_files(filt: str | None) -> list[Path]:
    """Every test in `tests/`, which mirrors the app tree.

    One recursive walk of one folder, rather than a scan that has to know how
    deep the apps are. The filter matches the app's own folder name — the same
    word you would type for the app itself, so `hub`, `pipeline` and
    `quit-trigger` all work.
    """
    if not TESTS.is_dir():
        return []
    found: list[Path] = []
    for f in sorted(TESTS.rglob("test_*.py")):
        if "__pycache__" in f.parts:
            continue
        if filt and filt.lower() not in f.parent.name.lower():
            continue
        found.append(f)
    return found


def run_one(path: Path, verbose: bool) -> tuple[bool, int, str]:
    """Returns (ok, n_tests, tail_of_output)."""
    cmd = [sys.executable, str(path)]
    if verbose:
        cmd.append("-v")
    proc = subprocess.run(
        cmd, cwd=str(path.parent), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    n = 0
    for line in out.splitlines():
        if line.startswith("Ran ") and " test" in line:
            try:
                n = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return proc.returncode == 0, n, out


def main() -> int:
    # A failing test's output is captured with errors="replace", so it can carry
    # U+FFFD. The Windows console is cp1252 and cannot encode that, so printing
    # the failure crashed the runner *instead of reporting the failure* — the
    # worst possible moment to lose your output. Never let encoding be the thing
    # that hides a test result.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    args = [a for a in sys.argv[1:]]
    verbose = "-v" in args
    filt = next((a for a in args if not a.startswith("-")), None)

    files = test_files(filt)
    if not files:
        print(f"{YELLOW}No test files found"
              f"{' matching ' + filt if filt else ''}.{RESET}")
        print(f"{DIM}Looked for */tests/test_*.py under {ROOT}{RESET}")
        return 1

    print(f"\n{BOLD}Running {len(files)} test file(s){RESET}\n")
    started = time.time()
    results = []

    for f in files:
        label = f"{f.parent.name}/{f.name}"
        sys.stdout.write(f"  {label:<46}")
        sys.stdout.flush()
        try:
            ok, n, out = run_one(f, verbose)
        except subprocess.TimeoutExpired:
            ok, n, out = False, 0, "TIMED OUT after 600s"
        results.append((label, ok, n, out))
        if ok:
            print(f"{GREEN}PASS{RESET} {DIM}({n} tests){RESET}")
        else:
            print(f"{RED}FAIL{RESET} {DIM}({n} tests){RESET}")

    elapsed = time.time() - started
    failed = [r for r in results if not r[1]]
    total = sum(r[2] for r in results)

    for label, ok, n, out in failed:
        print(f"\n{RED}{'=' * 66}{RESET}")
        print(f"{RED}{BOLD}FAILED: {label}{RESET}")
        print(f"{RED}{'=' * 66}{RESET}")
        print("\n".join(out.splitlines()[-60:]))

    print(f"\n{BOLD}{'-' * 66}{RESET}")
    if failed:
        print(f"{RED}{BOLD}  {len(failed)} of {len(results)} file(s) FAILED{RESET}"
              f"   {DIM}{total} tests, {elapsed:.1f}s{RESET}")
        return 1
    print(f"{GREEN}{BOLD}  ALL PASS{RESET}   {DIM}{total} tests across "
          f"{len(results)} files, {elapsed:.1f}s{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
