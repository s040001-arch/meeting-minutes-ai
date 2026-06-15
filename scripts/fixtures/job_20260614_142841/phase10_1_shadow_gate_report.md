# Phase 10.1 Shadow Gate Report — job_20260614_142841

## Verdict counts
{
  "ask_without_candidate": 24,
  "auto_correct": 8,
  "ask_with_candidate": 27
}
proposal_total: 59
fact_class_guard_downgrade_count: 5
gate would_fail: False
gate would_fail_rate: 0.0

## Spot checks
### 義理をか (1 hits)
- verdict=ask_without_candidate fact_class=proper_noun hypothesis=''
  span_before: あと工場がもう義理をかとか。あと八戸かな
### こうかだからな (1 hits)
- verdict=ask_without_candidate fact_class=filler_garble hypothesis=''
  span_before: 当初予定してた森川さんへの引き継ぎで泳いか多くしていこうかな
### 16時にちょっという (15 hits)
- verdict=ask_without_candidate fact_class=filler_garble hypothesis=''
  span_before: あの、イランニングがちょっと入ってくるって
  original_verdict: auto_correct routing_override: None
- verdict=auto_correct fact_class=lexical_fluency hypothesis=''
  span_before: 多分、あの従来引きずらってる自然課題ですね
- verdict=ask_with_candidate fact_class=proper_noun hypothesis='海老様'
  span_before: 今回ちょっとあのゆみ様にご相談してたのが

## fact_tokens_in_auto_verdict
[]

## Guard downgrades
{"proposal_id": "500f8bd8-afbb-4250-9416-82f377dc7a2e", "anomaly_word": "イランニング", "span_before": "あの、イランニングがちょっと入ってくるって", "original_verdict": "auto_correct", "verdict": "ask_without_candidate", "fact_class": "filler_garble", "hypothesis": ""}
{"proposal_id": "e986ca28-ff2a-4f0c-aa36-b071cbd3963c", "anomaly_word": "合っています", "span_before": "あのロジシンの業務と、合っていますロジシンの業務の中の課題", "original_verdict": "auto_correct", "verdict": "ask_without_candidate", "fact_class": "filler_garble", "hypothesis": ""}
{"proposal_id": "964755b0-1125-4c42-ad82-ae9aecd4b1b2", "anomaly_word": "合っています", "span_before": "合っていますその研修の参加の目的の意識をこう醸成", "original_verdict": "auto_correct", "verdict": "ask_without_candidate", "fact_class": "filler_garble", "hypothesis": ""}
{"proposal_id": "da60835d-de13-4b89-9bdb-c714ca6c7015", "anomaly_word": "優勝", "span_before": "ちょっとあの優勝になってしまうんです。けれども、事前課題だけであれば", "original_verdict": "auto_correct", "verdict": "ask_without_candidate", "fact_class": "filler_garble", "hypothesis": ""}
{"proposal_id": "deb73682-b165-409a-afac-d6801b045694", "anomaly_word": "故障", "span_before": "値段の故障の時の中にこう。ちょっと定石なんかも", "original_verdict": "auto_correct", "verdict": "ask_without_candidate", "fact_class": "filler_garble", "hypothesis": ""}