"""Repair mis-anchored anomaly_word on ③ proposals before export/apply."""
from __future__ import annotations

import re
from typing import Any

_NUMERIC_SHA_IN_SPAN = re.compile(r"(\d[\d,.]*)\s*車")
_SHA_COUNT_CONTEXT = re.compile(r"社|子会社|\d+\s*社")
_NUMERIC_SHIAI_IN_SPAN = re.compile(r"\d+\s*試合")
_SHIAI_BUSINESS_CONTEXT = re.compile(
    r"社|子会社|CCI|ばらま|事例|コープ|矢崎|案件|データ|やった|増える"
)

GARBLE_PATTERN_SHIAI_SHA = "shiai_to_sha"
SHIAI_GARBLE_TOKEN = "試合"


def _collapse(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def detect_numeric_sha_unit_mismatch(span_before: str, context: str) -> str | None:
    """Detect 「N 車」 in span when surrounding context discusses 社/子会社 counts."""
    sb = str(span_before or "").strip()
    ctx = str(context or "")
    if not sb or not ctx:
        return None
    m = _NUMERIC_SHA_IN_SPAN.search(sb)
    if not m:
        return None
    if not _SHA_COUNT_CONTEXT.search(ctx):
        return None
    return m.group(0).strip()


def detect_shiai_company_garble(span_before: str, context: str) -> bool:
    """True when 「N 試合」 appears in a company/case-counting context (not sports)."""
    sb = str(span_before or "").strip()
    ctx = str(context or "")
    if not sb or not _NUMERIC_SHIAI_IN_SPAN.search(sb):
        return False
    return bool(_SHIAI_BUSINESS_CONTEXT.search(sb + "\n" + ctx))


def shiai_pinpoint_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a ③ item to anomaly_word=試合 for shared 社 landing."""
    out = dict(item)
    sb = str(out.get("span_before") or "")
    ctx = str(out.get("context") or "")
    if not detect_shiai_company_garble(sb, ctx):
        return out
    aw = str(out.get("anomaly_word") or "").strip()
    if aw == SHIAI_GARBLE_TOKEN:
        out["garble_pattern"] = GARBLE_PATTERN_SHIAI_SHA
        return out
    if SHIAI_GARBLE_TOKEN not in sb:
        return out
    out["anomaly_word"] = SHIAI_GARBLE_TOKEN
    out["garble_pattern"] = GARBLE_PATTERN_SHIAI_SHA
    out.setdefault("anomaly_repair", [])
    if isinstance(out["anomaly_repair"], list):
        out["anomaly_repair"] = list(out["anomaly_repair"])
        out["anomaly_repair"].append(f"shiai_garble:{aw}->{SHIAI_GARBLE_TOKEN}")
    su = out.get("selected_unknown")
    if isinstance(su, dict):
        su2 = dict(su)
        su2["anomaly_word"] = SHIAI_GARBLE_TOKEN
        out["selected_unknown"] = su2
    return out


def expand_shiai_step3_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Emit shiai pinpoint rows; keep non-試合 anomalies (e.g. 小松放棄) as separate items."""
    out: list[dict[str, Any]] = []
    for item in items:
        sb = str(item.get("span_before") or "")
        ctx = str(item.get("context") or "")
        aw = str(item.get("anomaly_word") or "").strip()
        if detect_shiai_company_garble(sb, ctx):
            shiai = shiai_pinpoint_item(dict(item))
            shiai["question_text"] = ""  # rebuilt after bundle
            out.append(shiai)
            if aw and aw != SHIAI_GARBLE_TOKEN and "試合" not in aw:
                out.append(dict(item))
        else:
            out.append(item)
    return out


def repair_step3_anomaly_anchor(item: dict[str, Any]) -> dict[str, Any]:
    """Fix known anomaly_word mis-anchors (export/apply safe, idempotent)."""
    out = dict(item)
    sb = str(out.get("span_before") or "")
    ctx = str(out.get("context") or "")
    aw = str(out.get("anomaly_word") or "").strip()
    repairs: list[str] = []

    sha_token = detect_numeric_sha_unit_mismatch(sb, ctx)
    if sha_token and sha_token in sb and aw != sha_token:
        if "車" not in aw or _collapse(aw) != _collapse(sha_token):
            out["anomaly_word"] = sha_token
            out["fact_class"] = "numeric"
            repairs.append(f"numeric_unit_sha:{aw}->{sha_token}")
            su = out.get("selected_unknown")
            if isinstance(su, dict):
                su2 = dict(su)
                su2["anomaly_word"] = sha_token
                su2["fact_class"] = "numeric"
                out["selected_unknown"] = su2

    if repairs:
        out["anomaly_repair"] = repairs
    return out


def audit_anomaly_span_alignment(item: dict[str, Any]) -> list[str]:
    """Return human-readable issues when anomaly_word does not match span intent."""
    issues: list[str] = []
    aw = str(item.get("anomaly_word") or "").strip()
    sb = str(item.get("span_before") or "").strip()
    ctx = str(item.get("context") or "")

    if not aw:
        issues.append("missing_anomaly_word")
        return issues

    if aw not in sb and _collapse(aw) not in _collapse(sb):
        issues.append(f"anomaly_not_in_span: {aw!r}")

    sha_token = detect_numeric_sha_unit_mismatch(sb, ctx)
    if sha_token and aw != sha_token and "車" not in aw:
        issues.append(f"numeric_sha_misanchor: {aw!r} -> should be {sha_token!r}")

    if re.search(r"\d+\s*数字", sb) and "数字" not in aw and "数字" not in _collapse(aw):
        issues.append(f"numeric_digit_garble: anomaly should include 数字, got {aw!r}")

    if re.search(r"サイン数", sb) and aw != "サイン数":
        issues.append(f"headcount_attr_misanchor: {aw!r} -> should be サイン数")

    return issues
