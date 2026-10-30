"""Rule engine: runs the business invariants against the live database.

Plain code, no LLM: detection must be fast, cheap and exact.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "invariants.yaml"


@dataclass
class InvariantResult:
    id: str
    description: str
    services: list[str]
    violations: int

    @property
    def ok(self) -> bool:
        return self.violations == 0


class RuleEngine:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.rules = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    def check(self, db, now: int) -> list[InvariantResult]:
        results = []
        for r in self.rules:
            count = db.execute(r["query"], {"now": now}).fetchone()[0] or 0
            results.append(InvariantResult(r["id"], r["description"], r["services"], int(count)))
        return results

    def violations(self, db, now: int) -> list[InvariantResult]:
        return [r for r in self.check(db, now) if not r.ok]
