import sys
sys.stdout.reconfigure(encoding='utf-8')
p = '/app/scripts/fixtures/job_20260701_053826_ai_with_notes.txt'
with open(p, encoding='utf-8') as f:
    t = f.read()
supp = t.count('[補足:')
print(f'chars={len(t)} supp={supp}')
print(f'戦略推進室={t.count("戦略推進室")} 中本絵麻={t.count("中本絵麻")} 精度が高い={t.count("精度が高い")}')
print(f'大瀬さん={t.count("大瀬さん")} 人脈推進室={t.count("人脈推進室")} 制度高い={t.count("制度高い")}')
