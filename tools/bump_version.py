"""Set the version everywhere it lives, and start its CHANGELOG entry.

    python tools/bump_version.py 1.3.2

The version is in three places that must always match:
  custom_components/irrigation_sequencer/manifest.json   "version"   (what Home Assistant and HACS show)
  custom_components/irrigation_sequencer/const.py        VERSION     (device info, the ?v= on the card and page)
  .../frontend/irrigation-sequencer-card.js              CARD_VERSION (the browser console line)
A new "## <version> (<date>)" heading is added to the top of CHANGELOG.md if it is not there yet:
write what changed under it before you release (tools/release_check.py refuses an empty entry).
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "irrigation_sequencer"


def main(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"Version must look like 1.3.2, not {version!r}")

    manifest = PKG / "manifest.json"
    data = json.loads(manifest.read_text())
    data["version"] = version
    manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    const = PKG / "const.py"
    const.write_text(re.sub(r'^VERSION = "[^"]*"', f'VERSION = "{version}"', const.read_text(), count=1, flags=re.M))

    card = PKG / "frontend" / "irrigation-sequencer-card.js"
    card.write_text(re.sub(r'^const CARD_VERSION = "[^"]*";', f'const CARD_VERSION = "{version}";',
                           card.read_text(), count=1, flags=re.M))

    log = ROOT / "CHANGELOG.md"
    text = log.read_text()
    if not re.search(rf"^## {re.escape(version)}\b", text, flags=re.M):
        today = date.today()
        heading = f"## {version} ({today.day} {today:%b %Y})\n\n* \n\n"
        first = text.find("\n## ")
        text = text[: first + 1] + heading + text[first + 1:] if first >= 0 else text + "\n" + heading
        log.write_text(text)
        print(f"CHANGELOG.md: added '## {version}': write what changed under it")
    print(f"Version set to {version} in manifest.json, const.py and the card")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
