"""FortiOS version parsing and comparison."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_VER_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:[ ,]*(?:build|b)?\s*0*(\d+))?", re.I)


@total_ordering
@dataclass(frozen=True)
class FosVersion:
    major: int
    minor: int
    patch: int
    build: int | None = None

    @classmethod
    def parse(cls, text: str | "FosVersion" | None) -> "FosVersion":
        if isinstance(text, FosVersion):
            return text
        if not text:
            raise ValueError("empty version")
        m = _VER_RE.search(str(text))
        if not m:
            raise ValueError(f"cannot parse FortiOS version from {text!r}")
        b = int(m.group(4)) if m.group(4) else None
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)), b)

    @classmethod
    def from_parts(cls, d: dict) -> "FosVersion":
        return cls(int(d["major"]), int(d["minor"]), int(d["patch"]), d.get("build"))

    @property
    def train(self) -> tuple[int, int]:
        return (self.major, self.minor)

    def _key(self):
        return (self.major, self.minor, self.patch)

    def __lt__(self, other):
        return self._key() < FosVersion.parse(other)._key()

    def __eq__(self, other):
        try:
            return self._key() == FosVersion.parse(other)._key()
        except ValueError:
            return NotImplemented

    def __hash__(self):
        return hash(self._key())

    def __str__(self):
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def full(self) -> str:
        return f"v{self}" + (f" build{self.build:04d}" if self.build is not None else "")
