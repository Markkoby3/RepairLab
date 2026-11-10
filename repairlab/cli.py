"""Command line entry point: `python -m repairlab <command>`."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import llm
from .triage import dump_json, triage_files


def cmd_triage(args: argparse.Namespace) -> int:
    report = triage_files(args.junit)
    notes = llm.ai_notes(report) if args.ai else None
    md = report.to_markdown(ai_notes=notes)
    print(md)
    if args.md:
        Path(args.md).write_text(md)
    if args.json:
        Path(args.json).write_text(dump_json(report))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write(md)
    return report.exit_code if args.gate else 0


def cmd_fleet(args: argparse.Namespace) -> int:
    from .agent import Lab
    from .fleet import Fleet

    with Lab(per_model=args.per_model) as lab:
        fleet = Fleet(lab.inventory())
        for rec in lab.inventory():
            fleet.health_check(rec.serial)
        print(fleet.table())
        if args.json:
            print(json.dumps([r.to_dict() for r in lab.inventory()], indent=2))
        if args.serve:
            print("\nServing lab on localhost. Ctrl-C to stop.", flush=True)
            try:
                import time
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    return 0


def _pytest(junit: Path, attempt: int, extra: list[str], root: Path) -> int:
    env = {**os.environ, "REPAIRLAB_CHAOS": "1", "REPAIRLAB_ATTEMPT": str(attempt)}
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", f"--junitxml={junit}",
           "-o", f"cache_dir={junit.parent / '.pytest_cache'}", *extra, str(root / "tests")]
    return subprocess.run(cmd, env=env, cwd=root, stdout=subprocess.DEVNULL).returncode


def cmd_demo(args: argparse.Namespace) -> int:
    """Run the suite against a misbehaving lab, rerun failures, and triage both attempts."""
    root = Path(__file__).resolve().parent.parent
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    first, second = out / "attempt1.xml", out / "attempt2.xml"
    print("attempt 1: full suite with chaos faults …", file=sys.stderr)
    rc = _pytest(first, 1, [], root)
    junits = [first]
    if rc != 0:
        print("attempt 2: rerunning failures only …", file=sys.stderr)
        _pytest(second, 2, ["--lf"], root)
        junits.append(second)
    ns = argparse.Namespace(junit=junits, md=str(out / "triage.md"), json=str(out / "triage.json"),
                            ai=args.ai, gate=False)
    return cmd_triage(ns)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="repairlab", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("triage", help="classify failures from one or more JUnit XML attempts")
    t.add_argument("junit", nargs="+", type=Path, help="attempt1.xml [attempt2.xml ...] in order")
    t.add_argument("--md", help="write the markdown report here")
    t.add_argument("--json", help="write the JSON report here")
    t.add_argument("--ai", action="store_true", help="add LLM notes (needs ANTHROPIC_API_KEY)")
    t.add_argument("--gate", action="store_true", help="exit 1 on real failures, 2 on lab-only failures")
    t.set_defaults(func=cmd_triage)

    f = sub.add_parser("fleet", help="start a simulated lab and print fleet health")
    f.add_argument("--per-model", type=int, default=2)
    f.add_argument("--json", action="store_true")
    f.add_argument("--serve", action="store_true", help="keep the lab running")
    f.set_defaults(func=cmd_fleet)

    d = sub.add_parser("demo", help="chaos run + rerun + triage, end to end")
    d.add_argument("--out", default="triage-out")
    d.add_argument("--ai", action="store_true")
    d.set_defaults(func=cmd_demo)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
