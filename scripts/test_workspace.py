"""Run unittest suites in isolated processes to avoid duplicate tests package names."""

import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    suites = [root, *sorted((root / "packages").iterdir())]
    for suite in suites:
        if not (suite / "tests").is_dir():
            continue
        print(f"Testing {suite.relative_to(root)}", flush=True)
        subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            cwd=suite,
            check=True,
        )


if __name__ == "__main__":
    main()
