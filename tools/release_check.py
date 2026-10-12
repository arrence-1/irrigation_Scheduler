"""Check a release is consistent and print its release notes (from CHANGELOG.md).

    python tools/release_check.py 1.3.2 > notes.md

Fails (exit 1) when the three version strings differ from the one given, or when CHANGELOG.md has no
entry for it (or an empty one). The GitHub release workflow runs this before it publishes anything.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "irrigation_sequencer"


def versions() -> dict[str, str | None]:
    const = re.search(r'^VERSION = "([^"]*)"', (PKG / "const.py").read_text(), flags=re.M)
    card = re.search(r'^const CARD_VERSION = "([^"]*)";',
                     (PKG / "frontend" / "irrigation-sequencer-card.js").read_text(), flags=re.M)
    return {
        "manifest.json": json.loads((PKG / "manifest.json").read_text()).get("version"),
        "const.py": const.group(1) if const else None,
        "irrigation-sequencer-card.js": card.group(1) if card else None,
    }


def notes(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text()
    m = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    if not m:
        return ""
    body = m.group(1).strip()
    return "" if re.fullmatch(r"[*\-\s]*", body) else body


def main(version: str) -> None:
    version = version.removeprefix("v")
    problems = [f"{where} says {found!r}" for where, found in versions().items() if found != version]
    hint = f"Run: python tools/bump_version.py {version}" if problems else ""
    body = notes(version)
    if not body:
        problems.append(f"CHANGELOG.md has no (or an empty) '## {version}' entry: write what changed under it")
    if problems:
        sys.exit(f"Release {version} is not ready:\n  " + "\n  ".join(problems) + (f"\n{hint}" if hint else ""))
    print(body)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
