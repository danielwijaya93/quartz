#!/usr/bin/env python3
"""Bump the unified Quartz version across pyproject.toml and webapp/package.json."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def main() -> int:
    if len(sys.argv) != 2 or not SEMVER_RE.match(sys.argv[1]):
        print("Usage: scripts/bump_version.py <X.Y.Z>", file=sys.stderr)
        return 2

    new_version = sys.argv[1]
    root = Path(__file__).resolve().parents[1]

    pyproject = root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^(version\s*=\s*)"[^"]+"',
        rf'\1"{new_version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        print("Failed to update pyproject.toml", file=sys.stderr)
        return 1
    pyproject.write_text(updated, encoding="utf-8")

    pkg_path = root / "webapp" / "package.json"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    pkg["version"] = new_version
    pkg_path.write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")

    print(f"Updated Quartz version to {new_version} in pyproject.toml and webapp/package.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
