"""Optional LLM-written triage notes.

The deterministic triage engine decides categories; this module only asks a model to
explain likely root causes and next steps for the clusters it was given. Guardrails:

* off unless ANTHROPIC_API_KEY is set (CI forks and local runs work without it)
* only cluster signatures and trimmed samples are sent, never full logs
* the model must answer in JSON keyed by cluster id; ids it invents are dropped
* any network or parsing failure returns None and the report ships without notes
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable

from .triage import TriageReport

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"

Transport = Callable[[dict], dict]

SYSTEM = (
    "You are a senior test engineer triaging an automated hardware test run. "
    "Each cluster already has a category assigned by deterministic rules; do not change it. "
    "For each cluster give the most likely root cause and one concrete next step. "
    'Reply with JSON only: {"summary": str, "clusters": [{"id": str, "root_cause": str, "next_step": str}]}'
)


def build_prompt(report: TriageReport, max_sample: int = 600) -> str:
    clusters = [
        {
            "id": c.id,
            "category": c.category.value,
            "test": c.function,
            "signature": c.signature,
            "hits": len(c.nodeids),
            "devices": sorted(c.devices),
            "blast_radius": c.blast_radius,
            "sample": c.sample[:max_sample],
        }
        for c in report.clusters
    ]
    return json.dumps({"verdict": report.verdict, "passed": report.passed, "total": report.total,
                       "clusters": clusters}, indent=1)


def _http_transport(payload: dict) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def ai_notes(report: TriageReport, transport: Transport | None = None) -> str | None:
    if not report.clusters:
        return None
    if transport is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        transport = _http_transport
    payload = {
        "model": os.environ.get("REPAIRLAB_LLM_MODEL", DEFAULT_MODEL),
        "max_tokens": 1200,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": build_prompt(report)}],
    }
    try:
        raw = transport(payload)
        text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text")
        data = json.loads(_strip_fences(text))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, AttributeError, TypeError):
        return None
    return render(report, data)


def render(report: TriageReport, data: dict) -> str | None:
    known = {c.id: c for c in report.clusters}
    lines = []
    if isinstance(data.get("summary"), str) and data["summary"].strip():
        lines.append(data["summary"].strip())
    for item in data.get("clusters", []):
        cid = item.get("id") if isinstance(item, dict) else None
        if cid not in known:  # drop hallucinated or malformed cluster references
            continue
        lines.append(f"- **{cid}** ({known[cid].category.value}): {item.get('root_cause', '').strip()} "
                     f"→ {item.get('next_step', '').strip()}")
    return "\n".join(lines) or None


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()
