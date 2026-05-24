# Railway Volume 上のジョブ成果物をローカルに取得する（要: railway login 済み）
param(
    [Parameter(Mandatory = $true)]
    [string]$JobId,
    [string]$Service = "",
    [string]$OutDir = "data/transcriptions/_railway_fetch"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$remoteDir = "/app/data/transcriptions/$JobId"
$localDir = Join-Path $OutDir $JobId
New-Item -ItemType Directory -Force -Path $localDir | Out-Null

$files = @(
    "merged_transcript.txt",
    "merged_transcript_ai.txt",
    "correction_meta.json",
    "e2e_run_log.txt",
    "meeting_profile.json",
    "processing_visible_log.txt"
)

foreach ($name in $files) {
    $remote = "$remoteDir/$name"
    $local = Join-Path $localDir $name
    Write-Host "Fetching $remote ..."
    if ($Service) {
        railway run --service $Service -- sh -c "cat '$remote'" | Set-Content -Path $local -Encoding utf8
    } else {
        railway run -- sh -c "cat '$remote'" | Set-Content -Path $local -Encoding utf8
    }
}

Write-Host "`n=== Character counts ==="
foreach ($name in @("merged_transcript.txt", "merged_transcript_ai.txt")) {
    $p = Join-Path $localDir $name
    if (Test-Path $p) {
        $t = Get-Content -Path $p -Raw -Encoding utf8
        Write-Host "$name : $($t.Length) chars"
    }
}

$metaPath = Join-Path $localDir "correction_meta.json"
if (Test-Path $metaPath) {
    Write-Host "`n=== correction_meta.json ==="
    Get-Content -Path $metaPath -Raw -Encoding utf8
} else {
    Write-Host "`n[WARN] correction_meta.json missing — likely pre-1cf1fc7 deploy or job failed before Step 4.3"
}

$docsLog = Join-Path $localDir "docs_write_log.txt"
if (Test-Path $docsLog) {
    Write-Host "`n=== docs_write_log.txt ==="
    Get-Content -Path $docsLog -Encoding utf8
}

foreach ($name in @("minutes_draft.md", "minutes_structured.md")) {
    $p = Join-Path $localDir $name
    if (Test-Path $p) {
        $lines = Get-Content -Path $p -Encoding utf8
        Write-Host "`n=== $name tail (last 20 lines) ==="
        $lines | Select-Object -Last 20
    }
}

Write-Host "`n=== e2e_run_log Step 4.3 ==="
$logPath = Join-Path $localDir "e2e_run_log.txt"
if (Test-Path $logPath) {
    Select-String -Path $logPath -Pattern "step_4_3|pipeline_build|correct_full_text" | ForEach-Object { $_.Line }
}

Write-Host "`nSaved under $localDir"
