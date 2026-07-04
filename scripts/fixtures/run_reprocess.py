import subprocess, sys, os, glob
sys.stdout.reconfigure(encoding='utf-8')

# Find the job dir
base = '/app/data/transcriptions'
matches = glob.glob(base + '/job_20260701_053826_*')
if not matches:
    print('ERROR: job dir not found')
    sys.exit(1)
job_dir = matches[0]
print(f'job_dir={job_dir}')

input_override = '/app/scripts/fixtures/job_20260701_053826_ai_with_notes.txt'
if not os.path.exists(input_override):
    print(f'ERROR: input override not found: {input_override}')
    sys.exit(1)

cmd = [
    'python3', '/app/reprocess_job.py',
    '--job-dir', job_dir,
    '--from-step', '6.1',
    '--input-override', input_override,
    '--reason', 'step3_typeA_pinpoint_supp5',
]
print(f'Running: {" ".join(cmd)}')
result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
print('=== STDOUT ===')
print(result.stdout[-8000:] if len(result.stdout) > 8000 else result.stdout)
if result.returncode != 0:
    print('=== STDERR ===')
    print(result.stderr[-2000:])
print(f'exit_code={result.returncode}')
