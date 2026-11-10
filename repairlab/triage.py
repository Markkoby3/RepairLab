"""Failure triage for automated test runs.

Input: one or more JUnit XML files from successive attempts of the same run
(attempt 1, then a rerun of the failures). Output: every failure sorted into

    PRODUCT  – the device or the software under test is wrong   -> file a bug
    TEST     – the test or fixture itself is broken              -> fix the test
    INFRA    – the lab could not reach or lease a device         -> fix the lab
    FLAKY    – failed, then passed on rerun with no code change  -> track & stabilize
    UNKNOWN  – no rule matched                                   -> a human looks

Failures are clustered by a normalized signature (device serials, test ids and numbers
replaced by placeholders), and each cluster gets a blast-radius read:
one device failing many tests points at hardware; one test failing on many devices
points at a software regression. Classification is deterministic; the optional LLM
summary in llm.py is advisory and can never change a category.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path


class Category(str, Enum):
    PRODUCT = "product"
    TEST = "test"
    INFRA = "infra"
    FLAKY = "flaky"
    UNKNOWN = "unknown"


# Ordered: the first rule that matches wins. (pattern, category, rule name)
RULES: list[tuple[re.Pattern[str], Category, str]] = [
    (re.compile(r"\b(DeviceTimeout|DeviceDisconnected|DeviceTransportError|LeaseTimeout|"
                r"ConnectionRefusedError|ConnectionResetError|No healthy device)\b"), Category.INFRA, "lab-transport"),
    (re.compile(r"fixture '[^']+' not found|\b(NameError|ImportError|ModuleNotFoundError|SyntaxError|"
                r"AttributeError|TypeError|KeyError|IndexError)\b"), Category.TEST, "test-code-error"),
    (re.compile(r"\b(AssertionError|DeviceCommandError|IllegalTransition)\b|^assert |\bassert\b"),
     Category.PRODUCT, "assertion"),
]

DEVICE_RE = re.compile(r"\bDV[A-Z]\d{6}\b")
TEST_ID_RE = re.compile(r"\b(battery|display|camera|biometric|speaker|keyboard)\.[a-z_]+\b")
NUM_RE = re.compile(r"(?<![A-Za-z_])-?\d+(\.\d+)?")
HEX_RE = re.compile(r"0x[0-9a-fA-F]+")


@dataclass
class Outcome:
    nodeid: str
    status: str  # passed | failed | error | skipped
    message: str = ""
    details: str = ""
    duration: float = 0.0

    @property
    def function(self) -> str:
        return self.nodeid.split("[", 1)[0]

    @property
    def devices(self) -> set[str]:
        return set(DEVICE_RE.findall(self.nodeid)) or set(DEVICE_RE.findall(self.message))


def parse_junit(path: str | Path) -> list[Outcome]:
    root = ET.parse(path).getroot()
    outcomes = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        module = classname.replace(".", "/") + ".py" if classname else ""
        nodeid = f"{module}::{case.get('name')}" if module else case.get("name", "")
        status, message, details = "passed", "", ""
        for tag in ("failure", "error", "skipped"):
            el = case.find(tag)
            if el is not None:
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = el.get("message", "") or ""
                details = (el.text or "").strip()
                break
        outcomes.append(Outcome(nodeid, status, message, details, float(case.get("time", 0) or 0)))
    return outcomes


def classify(o: Outcome) -> tuple[Category, str]:
    text = f"{o.message}\n{o.details}"
    # Errors raised while setting up a fixture are test-infrastructure problems unless
    # they are lab transport errors, which the INFRA rule catches first.
    for pattern, category, name in RULES:
        if pattern.search(text):
            if category == Category.PRODUCT and o.status == "error":
                return Category.TEST, "setup-error"
            return category, name
    return Category.UNKNOWN, "no-rule"


def signature(o: Outcome) -> str:
    """A normalized one-line signature so the same failure on different devices clusters."""
    first = (o.message or o.details or "").strip().splitlines()[0] if (o.message or o.details) else ""
    s = DEVICE_RE.sub("<DEVICE>", first)
    s = TEST_ID_RE.sub("<DIAG>", s)
    s = HEX_RE.sub("<HEX>", s)
    s = NUM_RE.sub("<N>", s)
    return s[:200]


@dataclass
class Cluster:
    id: str
    category: Category
    rule: str
    signature: str
    function: str
    nodeids: list[str] = field(default_factory=list)
    devices: set[str] = field(default_factory=set)
    sample: str = ""

    @property
    def blast_radius(self) -> str:
        n_dev, n_tests = len(self.devices), len(self.nodeids)
        if n_dev == 1 and n_tests > 1:
            return "single-device"
        if n_dev > 1:
            return "multi-device"
        return "isolated"

    @property
    def next_step(self) -> str:
        dev = ", ".join(sorted(self.devices)) or "n/a"
        return {
            (Category.PRODUCT, "single-device"): f"Suspect hardware on {dev}: rerun on a sibling unit, then pull the device for bench diagnosis.",
            (Category.PRODUCT, "multi-device"): "Same failure on several units: likely a software regression. Bisect recent changes and file a bug.",
            (Category.PRODUCT, "isolated"): f"Single failure on {dev}: reproduce on another unit before filing.",
            (Category.INFRA, "single-device"): f"Lab issue on {dev}: check cable/power, run fleet recovery, quarantine if it stays down.",
            (Category.INFRA, "multi-device"): "Several devices unreachable: check the lab host, hubs, and network before touching tests.",
            (Category.INFRA, "isolated"): f"One-off transport error on {dev}: rerun; quarantine if it repeats.",
            (Category.FLAKY, "single-device"): f"Intermittent on {dev}: track flake rate; check the sensor/fixture on that unit.",
            (Category.FLAKY, "multi-device"): "Intermittent across units: look for timing assumptions or shared state in the test.",
            (Category.FLAKY, "isolated"): "Passed on rerun: log it, and stabilize if it recurs.",
        }.get((self.category, self.blast_radius),
              "Fix the test or fixture code; it fails before exercising the device." if self.category == Category.TEST
              else "No rule matched: needs manual review.")


@dataclass
class TriageReport:
    total: int
    passed: int
    clusters: list[Cluster]
    attempts: int

    def count(self, category: Category) -> int:
        return sum(len(c.nodeids) for c in self.clusters if c.category == category)

    @property
    def verdict(self) -> str:
        if any(self.count(c) for c in (Category.PRODUCT, Category.TEST, Category.UNKNOWN)):
            return "red"
        if self.count(Category.INFRA):
            return "infra"
        if self.count(Category.FLAKY):
            return "green-with-flakes"
        return "green"

    @property
    def exit_code(self) -> int:
        """0 = ship it (flakes allowed), 1 = real failures, 2 = lab problem only."""
        return {"green": 0, "green-with-flakes": 0, "red": 1, "infra": 2}[self.verdict]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "total": self.total,
            "passed": self.passed,
            "attempts": self.attempts,
            "counts": {c.value: self.count(c) for c in Category},
            "clusters": [
                {**{k: v for k, v in asdict(c).items() if k != "devices"},
                 "category": c.category.value, "devices": sorted(c.devices),
                 "blast_radius": c.blast_radius, "next_step": c.next_step}
                for c in self.clusters
            ],
        }

    def to_markdown(self, ai_notes: str | None = None) -> str:
        icon = {"green": "✅", "green-with-flakes": "🟡", "infra": "🟠", "red": "🔴"}[self.verdict]
        lines = [
            f"## {icon} Test triage: {self.verdict}",
            "",
            f"{self.passed}/{self.total} passed after {self.attempts} attempt(s).",
            "",
            "| Category | Failures |",
            "|---|---|",
        ]
        lines += [f"| {c.value} | {self.count(c)} |" for c in Category if self.count(c)]
        order = [Category.PRODUCT, Category.TEST, Category.UNKNOWN, Category.INFRA, Category.FLAKY]
        for cl in sorted(self.clusters, key=lambda c: (order.index(c.category), -len(c.nodeids))):
            lines += [
                "",
                f"### [{cl.category.value}] {cl.id}: {cl.function.split('::')[-1]}",
                f"- **Signature:** `{cl.signature or '(no message)'}`",
                f"- **Hits:** {len(cl.nodeids)} test(s) on {', '.join(sorted(cl.devices)) or 'no specific device'}"
                f" ({cl.blast_radius})",
                f"- **Next step:** {cl.next_step}",
            ]
            lines += [f"  - `{n}`" for n in cl.nodeids[:5]]
            if len(cl.nodeids) > 5:
                lines.append(f"  - …and {len(cl.nodeids) - 5} more")
        if ai_notes:
            lines += ["", "### AI triage notes (advisory, does not change categories)", "", ai_notes]
        return "\n".join(lines) + "\n"


def triage(attempts: list[list[Outcome]]) -> TriageReport:
    """Triage a run given outcomes from attempt 1, attempt 2, ... (later attempts may be partial)."""
    if not attempts:
        raise ValueError("need at least one attempt")
    first = attempts[0]
    final: dict[str, Outcome] = {o.nodeid: o for o in first}
    ever_failed: dict[str, Outcome] = {}
    for run in attempts:
        for o in run:
            if o.status in ("failed", "error"):
                ever_failed.setdefault(o.nodeid, o)
            final[o.nodeid] = o

    groups: dict[tuple[str, str, str], Cluster] = {}
    for nodeid, first_failure in ever_failed.items():
        last = final[nodeid]
        if last.status == "passed":
            category, rule, source = Category.FLAKY, "passed-on-rerun", first_failure
        else:
            category, rule = classify(last)
            source = last
        sig = signature(source)
        # Lab problems cut across tests, so INFRA clusters by signature alone.
        key = (category.value, "" if category == Category.INFRA else source.function, sig)
        if key not in groups:
            groups[key] = Cluster(id=f"C{len(groups) + 1}", category=category, rule=rule, signature=sig,
                                  function=source.function, sample=(source.details or source.message)[:1500])
        groups[key].nodeids.append(nodeid)
        groups[key].devices |= source.devices

    counted = [o for o in final.values() if o.status != "skipped"]
    passed = sum(1 for o in counted if o.status == "passed")
    return TriageReport(total=len(counted), passed=passed, clusters=list(groups.values()), attempts=len(attempts))


def triage_files(paths: list[str | Path]) -> TriageReport:
    return triage([parse_junit(p) for p in paths])


def dump_json(report: TriageReport) -> str:
    return json.dumps(report.to_dict(), indent=2)
