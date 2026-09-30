"""Validation issues. Every check appends here instead of raising, so the caller
(usually an AI agent) gets the complete list of problems in one round."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Issue:
    severity: str          # "error" | "warning"
    path: str              # location in the input, e.g. "trade.legs[0].strike"
    message: str
    hint: str = ""

    def __str__(self):
        s = f"[{self.severity.upper()}] {self.path}: {self.message}"
        return s + (f"\n    hint: {self.hint}" if self.hint else "")


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    def _add(self, issue):
        if issue not in self.issues:        # components may be visited more than once
            self.issues.append(issue)

    def error(self, path, message, hint=""):
        self._add(Issue("error", path, message, hint))

    def warning(self, path, message, hint=""):
        self._add(Issue("warning", path, message, hint))

    def extend_pydantic(self, prefix: str, exc):
        """Translate a pydantic ValidationError into issues with full input paths."""
        for e in exc.errors():
            loc = prefix
            for part in e["loc"]:
                loc += f"[{part}]" if isinstance(part, int) else f".{part}"
            msg = e["msg"]
            if e["type"] == "extra_forbidden":
                msg = "unknown field (check spelling; see `price.py catalog`)"
            self.error(loc, msg)

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self):
        return not self.errors

    def to_dict(self):
        return {"ok": self.ok, "errors": [asdict(i) for i in self.errors],
                "warnings": [asdict(i) for i in self.warnings]}


class SpecError(Exception):
    """Raised when a spec has validation errors; carries the full report."""

    def __init__(self, report: Report):
        self.report = report
        super().__init__("\n".join(str(i) for i in report.errors))
