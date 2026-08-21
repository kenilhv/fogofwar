param([int]$StartAt = 25)

# Supervisor for the full-haystack ingest campaign.
#
# Ingesting all 500 instances is a long-running job. Rather than depend on a
# single process surviving it, this runs the work in short strides, each in a
# fresh Python process, retrying a stride that exits non-zero. Every write is
# an idempotent MERGE, so a repeated stride is harmless and the campaign
# converges regardless of transient failures.
#
# Usage: powershell -File scripts\run_campaign.ps1 [-StartAt <index>]

$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".venv\Scripts\python.exe"
$data = "data\raw\longmemeval_s_cleaned.json"
$stride = 50
$total = 500
$maxRetries = 3
$log = "run_campaign.log"

"campaign start (from instance $StartAt) $(Get-Date -Format o)" | Tee-Object $log -Append

for ($start = $StartAt; $start -lt $total; $start += $stride) {
    $end = [Math]::Min($start + $stride, $total)
    $ok = $false
    for ($attempt = 1; $attempt -le $maxRetries; $attempt++) {
        "stride $start-$end attempt $attempt $(Get-Date -Format o)" | Tee-Object $log -Append
        & $py scripts\run_demo.py --data $data --limit $total `
            --start-instance $start --end-instance $end `
            --ingest-chunk 25 --throttle-s 2 --no-eval 2>&1 |
            Tee-Object $log -Append
        if ($LASTEXITCODE -eq 0) { $ok = $true; break }
        "stride $start-$end attempt $attempt FAILED (exit $LASTEXITCODE)" | Tee-Object $log -Append
        Start-Sleep -Seconds 5
    }
    if (-not $ok) {
        "CAMPAIGN ABORTED at stride $start-$end after $maxRetries attempts" | Tee-Object $log -Append
        exit 1
    }
}

"ingest complete, running eval $(Get-Date -Format o)" | Tee-Object $log -Append
& $py scripts\run_demo.py --data $data --limit $total --skip-ingest --ablation 2>&1 |
    Tee-Object $log -Append
"campaign done (eval exit $LASTEXITCODE) $(Get-Date -Format o)" | Tee-Object $log -Append
exit $LASTEXITCODE
