#!/usr/bin/env python3
"""Phase 10.2.3 stage-1: 164142 editor apply (①④) + full transcript diff (no deploy)."""
from __future__ import annotations

import difflib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOB_GLOB = "job_20260330_164142*"
OUT_DIR = ROOT / "scripts" / "fixtures" / "phase10_2_3_164142_stage1"
BACKUP_DIR = OUT_DIR / "job_backup"


def _job_dir() -> Path:
    return next((ROOT / "data" / "transcriptions").glob(JOB_GLOB))


def _backup(job_dir: Path) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for name in (
        "merged_transcript_ai.txt",
        "merged_transcript_after_qa.txt",
        "editor_apply_report.json",
    ):
        src = job_dir / name
        if src.is_file():
            shutil.copy2(src, BACKUP_DIR / name)


def _restore(job_dir: Path) -> None:
    for name in (
        "merged_transcript_ai.txt",
        "merged_transcript_after_qa.txt",
        "editor_apply_report.json",
    ):
        src = BACKUP_DIR / name
        if src.is_file():
            shutil.copy2(src, job_dir / name)


def _run_apply(job_id: str) -> subprocess.CompletedProcess[str]:
    env = dict(**__import__("os").environ)
    env["SEMANTIC_INTEGRITY_GATE_ENABLED"] = "on"
    env["CONTEXTUAL_EDITOR_ENABLED"] = "1"
    cmd = [
        sys.executable,
        str(ROOT / "contextual_editor.py"),
        "--job-id",
        job_id,
        "--mode",
        "apply",
        "--apply-only",
        "--force",
    ]
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main() -> int:
    job_dir = _job_dir()
    job_id = job_dir.name
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    mechanical = (job_dir / "merged_transcript_mechanical.txt").read_text(encoding="utf-8")
    _backup(job_dir)

    proc = _run_apply(job_id)
    (OUT_DIR / "contextual_editor_stdout.json").write_text(
        proc.stdout or "", encoding="utf-8"
    )
    if proc.stderr:
        (OUT_DIR / "contextual_editor_stderr.txt").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        _restore(job_dir)
        return proc.returncode

    after = (job_dir / "merged_transcript_ai.txt").read_text(encoding="utf-8")
    apply_report_path = job_dir / "editor_apply_report.json"
    if not apply_report_path.is_file():
        print("missing editor_apply_report.json", file=sys.stderr)
        _restore(job_dir)
        return 1
    apply_report = json.loads(apply_report_path.read_text(encoding="utf-8"))

    diff_lines = list(
        difflib.unified_diff(
            mechanical.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="merged_transcript_mechanical.txt (BEFORE ①④)",
            tofile="merged_transcript_ai.txt (AFTER ①④)",
            n=0,
        )
    )
    diff_text = "".join(diff_lines)
    diff_path = OUT_DIR / "phase10_2_3_164142_editor_apply_full.diff"
    diff_path.write_text(diff_text, encoding="utf-8")

    props = json.loads((job_dir / "edit_proposals.json").read_text(encoding="utf-8"))["proposals"]
    auto_counts = {"auto_correct": 0, "auto_delete": 0}
    ask_counts = {"ask_with_candidate": 0, "ask_without_candidate": 0}
    for p in props:
        v = str(p.get("verdict") or "")
        if v in auto_counts:
            auto_counts[v] += 1
        elif v in ask_counts:
            ask_counts[v] += 1

    md = [
        "# Phase 10.2.3 Stage 1 — 164142 editor apply (①④) diff",
        "",
        f"- generated_at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- job: `{job_id}`",
        f"- mode: apply-only (existing edit_proposals.json, routing refresh)",
        f"- SEMANTIC_INTEGRITY_GATE_ENABLED: **on** (2-d production policy preview)",
        "",
        "## Summary",
        "",
        f"- BEFORE chars: {len(mechanical)}",
        f"- AFTER chars: {len(after)}",
        f"- char delta: {len(after) - len(mechanical):+d}",
        f"- proposals total: {len(props)} (① {auto_counts['auto_correct']} / ④ {auto_counts['auto_delete']} / "
        f"② {ask_counts['ask_with_candidate']} / ③ {ask_counts['ask_without_candidate']})",
        f"- applied_count: {apply_report.get('applied_count')}",
        f"- semantic_reverted_count: {apply_report.get('semantic_reverted_count')}",
        f"- fact_reverted_count: {apply_report.get('fact_reverted_count')}",
        f"- skipped_count: {apply_report.get('skipped_count')}",
        f"- gate_final.ok: {apply_report.get('gate_final', {}).get('ok')}",
        "",
        "## Applied (①④ only)",
        "",
    ]
    for entry in apply_report.get("applied") or []:
        md.append(f"### [{entry.get('verdict')}] {entry.get('anomaly_word')}")
        md.append(f"- before: `{entry.get('span_before')}`")
        if entry.get("verdict") == "auto_correct":
            md.append(f"- after: `{entry.get('span_after')}`")
        else:
            md.append("- after: *(deleted)*")
        md.append("")

    reverted = apply_report.get("reverted") or []
    if reverted:
        md.append("## Reverted (fact / semantic)")
        md.append("")
        for r in reverted:
            layer = r.get("revert_layer") or "?"
            md.append(
                f"- [{layer}] {r.get('verdict')} `{r.get('span_before')}` "
                f"error={r.get('apply_error')!r}"
            )
        md.append("")

    skipped = apply_report.get("skipped") or []
    if skipped:
        md.append("## Skipped")
        md.append("")
        for s in skipped:
            md.append(
                f"- {s.get('verdict')} `{s.get('proposal_id')}` error={s.get('apply_error')!r}"
            )
        md.append("")

    md.extend(
        [
            "## Full unified diff",
            "",
            f"See `{diff_path.name}` ({len(diff_lines)} diff lines).",
            "",
            "```diff",
            diff_text if diff_text else "(no changes)",
            "```",
        ]
    )

    summary_path = OUT_DIR / "phase10_2_3_164142_stage1_apply_report.md"
    summary_path.write_text("\n".join(md), encoding="utf-8")
    shutil.copy2(apply_report_path, OUT_DIR / "editor_apply_report.json")

    _restore(job_dir)

    meta = {
        "job_id": job_id,
        "applied_count": apply_report.get("applied_count"),
        "semantic_reverted_count": apply_report.get("semantic_reverted_count"),
        "fact_reverted_count": apply_report.get("fact_reverted_count"),
        "skipped_count": apply_report.get("skipped_count"),
        "gate_final_ok": apply_report.get("gate_final", {}).get("ok"),
        "before_chars": len(mechanical),
        "after_chars": len(after),
        "diff_lines": len(diff_lines),
        "diff_path": str(diff_path),
        "summary_path": str(summary_path),
        "job_files_restored": True,
    }
    (OUT_DIR / "stage1_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
