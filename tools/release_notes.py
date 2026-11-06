"""Print the GitHub release title or notes for the version in fortiupgrade/__init__.py.

    python tools/release_notes.py --title   -> FortiGate Upgrader v0.11.0 build0018
    python tools/release_notes.py           -> markdown notes from changelog.json
    python tools/release_notes.py --check v0.11.0   -> exit 1 unless the tag matches __version__
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
init = (ROOT / "fortiupgrade" / "__init__.py").read_text()
VER = re.search(r'__version__ = "([^"]+)"', init).group(1)
BUILD = int(re.search(r"__build__ = (\d+)", init).group(1))
LOG = json.loads((ROOT / "fortiupgrade" / "changelog.json").read_text())
KIND = {"new": "New", "improved": "Improved", "changed": "Changed", "fixed": "Fixed"}

if "--check" in sys.argv:
    tag = sys.argv[sys.argv.index("--check") + 1]
    ok = tag.lstrip("v") == VER and LOG[0]["version"] == VER and LOG[0]["build"] == BUILD
    print(f"tag {tag}, __version__ {VER} build {BUILD}, changelog top {LOG[0]['version']} build {LOG[0]['build']}")
    sys.exit(0 if ok else 1)
if "--title" in sys.argv:
    print(f"FortiGate Upgrader v{VER} build{BUILD:04d}")
    sys.exit(0)
rel = LOG[0]
out = [f"## {rel['title']}", ""]
for kind in ("new", "improved", "changed", "fixed"):
    items = [c["text"] for c in rel["changes"] if c["type"] == kind]
    if items:
        out += [f"**{KIND[kind]}**", *[f"- {t}" for t in items], ""]
out += ["### Downloads",
        "- **macOS** (Apple silicon): `FortiGate-Upgrader-%s-macOS-arm64.dmg`. Open it and drag the app to Applications." % VER,
        "- **macOS** (Intel): `FortiGate-Upgrader-%s-macOS-x86_64.dmg`, when present." % VER,
        "- **Windows** 10/11 x64: `FortiGate-Upgrader-%s-Windows-x64-Setup.exe`. Installs for the current user." % VER,
        "- `SHA256SUMS.txt`: checksums. The app verifies these before installing an in-app update.",
        "",
        "The apps are not code-signed. First launch on macOS: right-click the app › Open. "
        "On Windows, SmartScreen may ask you to confirm (More info › Run anyway). "
        "Updates installed from inside the app don't show these prompts again.",
        "",
        "Full history: System › Change log in the app, or `fortiupgrade/changelog.json`."]
print("\n".join(out))
