#!/usr/bin/env python3
"""Write Phase 10.1 shadow gate report for job_20260614_142841."""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
JOB_GLOB = "job_20260614_142841*"


def main() -> int:
    job_dirs = list((ROOT / "data" / "transcriptions").glob(JOB_GLOB))
    if not job_dirs:
        print("job dir not found under data/transcriptions")
        return 1
    job_dir = job_dirs[0]
    report = json.loads((job_dir / "editor_shadow_report.json").read_text(encoding="utf-8"))
    props = json.loads((job_dir / "edit_proposals.json").read_text(encoding="utf-8"))["proposals"]

    lines: list[str] = []
    lines.append("# Phase 10.1 Shadow Gate Report — job_20260614_142841")
    lines.append("")
    lines.append("## Verdict counts")
    lines.append(json.dumps(report["verdict_counts"], ensure_ascii=False, indent=2))
    lines.append(f"proposal_total: {report['proposal_total']}")
    lines.append(f"fact_class_guard_downgrade_count: {report['fact_class_guard_downgrade_count']}")
    sim = report["gate_simulation"]
    lines.append(f"gate would_fail: {sim['would_fail']}")
    lines.append(f"gate would_fail_rate: {sim['would_fail_rate']}")
    lines.append("")

    checks = [
        ("義理をか", lambda pr: "義理" in f"{pr.get('span_before', '')}{pr.get('evidence', '')}"),
        ("こうかだからな", lambda pr: "こうか" in f"{pr.get('span_before', '')}{pr.get('evidence', '')}"),
        ("16時にちょっという", lambda pr: "ちょっ" in f"{pr.get('span_before', '')}{pr.get('evidence', '')}"),
    ]
    lines.append("## Spot checks")
    for label, fn in checks:
        hits = [pr for pr in props if fn(pr)]
        lines.append(f"### {label} ({len(hits)} hits)")
        for h in hits[:3]:
            lines.append(
                f"- verdict={h['verdict']} fact_class={h['fact_class']} "
                f"hypothesis={h.get('hypothesis')!r}"
            )
            sb = str(h.get("span_before") or "")[:100]
            lines.append(f"  span_before: {sb}")
            if h.get("original_verdict"):
                lines.append(
                    f"  original_verdict: {h['original_verdict']} "
                    f"routing_override: {h.get('routing_override')}"
                )

    lines.append("")
    lines.append("## fact_tokens_in_auto_verdict")
    lines.append(
        json.dumps(report["spot_checks"]["fact_tokens_in_auto_verdict"], ensure_ascii=False, indent=2)
    )
    lines.append("")
    lines.append("## Guard downgrades")
    for d in report["fact_class_guard_downgrades"]:
        lines.append(json.dumps(d, ensure_ascii=False))

    out = ROOT / "scripts" / "fixtures" / "job_20260614_142841" / "phase10_1_shadow_gate_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
