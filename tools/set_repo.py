"""Point the links at your GitHub repository (run once, before the first upload).

    python tools/set_repo.py YOUR_GITHUB_NAME [repository-name]

Replaces the placeholder GITHUB_USER (and the repository name, if you use another one than
irrigation_sequencer) in manifest.json (documentation, issue tracker, code owner), the card, the README
and the docs. HACS refuses an integration whose manifest has no documentation / issue_tracker / codeowners.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "GITHUB_USER"
FILES = ["README.md", "CHANGELOG.md", "custom_components/irrigation_sequencer/manifest.json",
         "custom_components/irrigation_sequencer/frontend/irrigation-sequencer-card.js", "docs/*.md"]


def main(owner: str, repo: str = "irrigation_sequencer") -> None:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner):
        sys.exit(f"{owner!r} does not look like a GitHub user name")
    changed = []
    for pattern in FILES:
        for path in ROOT.glob(pattern):
            text = path.read_text()
            new = text.replace(f"{PLACEHOLDER}/irrigation_sequencer", f"{owner}/{repo}").replace(PLACEHOLDER, owner)
            if new != text:
                path.write_text(new)
                changed.append(str(path.relative_to(ROOT)))
    print("Updated:", ", ".join(changed) if changed else "nothing (already set?)")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    main(*sys.argv[1:])
