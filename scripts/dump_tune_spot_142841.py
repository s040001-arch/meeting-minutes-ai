#!/usr/bin/env python3
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
p = list((ROOT / "data" / "transcriptions").glob("job_20260614_142841*"))[0]
props = json.loads((p / "edit_proposals.json").read_text(encoding="utf-8"))["proposals"]
report = json.loads((p / "editor_shadow_report.json").read_text(encoding="utf-8"))

out = {
    "verdict_counts": report["verdict_counts"],
    "before_tune": {"ask_with_candidate": 27, "ask_without_candidate": 24, "auto_correct": 8, "auto_delete": 0},
    "gate": report["gate_simulation"],
    "guard_downgrades": report["fact_class_guard_downgrade_count"],
    "A_giri": [x for x in props if "義理" in x.get("span_before", "")],
    "B_16cho": [x for x in props if "16" in x.get("span_before", "") and "ちょ" in x.get("span_before", "")],
    "auto_delete_samples": [x for x in props if x["verdict"] == "auto_delete"][:5],
    "fact_tokens_in_auto": report["spot_checks"]["fact_tokens_in_auto_verdict"],
}
path = ROOT / "scripts" / "fixtures" / "job_20260614_142841" / "tune_spot.json"
path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(path)
