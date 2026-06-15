# Phase 10.1 Shadow Gate Report — job_20260614_142841

## Verdict counts
{
  "auto_delete": 6,
  "ask_with_candidate": 18,
  "auto_correct": 6,
  "ask_without_candidate": 5
}
proposal_total: 35
fact_class_guard_downgrade_count: 2
gate would_fail: True
gate would_fail_rate: 1.0

## Spot checks
### 義理をか (1 hits)
- verdict=ask_with_candidate fact_class=proper_noun hypothesis='盛岡'
  span_before: 工場がもう義理をかとか。あと八戸かなふふふ
### こうかだからな (1 hits)
- verdict=auto_delete fact_class=filler_garble hypothesis=''
  span_before: 森川さんへの引き継ぎで泳いか多くしていこうかなとあ回ってました
### 16時にちょっという (12 hits)
- verdict=auto_delete fact_class=filler_garble hypothesis=''
  span_before: あの、イランニングがちょっと入ってくるってところもあるので
  original_verdict: auto_correct routing_override: filler_to_delete
- verdict=auto_correct fact_class=lexical_fluency hypothesis=''
  span_before: あの従来引きずらってる自然課題ですね
- verdict=ask_with_candidate fact_class=proper_noun hypothesis='海老様'
  span_before: 今回ちょっとあのゆみ様にご相談してたのが

## fact_tokens_in_auto_verdict
[
  {
    "proposal_id": "bbd65265-2a96-4158-9a88-5c1aa8e43665",
    "verdict": "auto_correct",
    "fact_class": "lexical_fluency",
    "span_before": "あそこ横浜のやっぱあの多分使うかなとうん処置しました"
  }
]

## Guard downgrades
{"proposal_id": "dab97f28-2e3d-4698-8b34-56025568b26b", "anomaly_word": "イランニング", "span_before": "あの、イランニングがちょっと入ってくるってところもあるので", "original_verdict": "auto_correct", "verdict": "auto_delete", "fact_class": "filler_garble", "hypothesis": ""}
{"proposal_id": "68dc6632-d22c-4eb6-967f-319cdcec3551", "anomaly_word": "3 人目研修", "span_before": "言ってしまうと新入社員フォローアップの検出はい?え、2 年目のなったので 3 人目研修にはい", "original_verdict": "auto_correct", "verdict": "ask_without_candidate", "fact_class": "numeric", "hypothesis": ""}