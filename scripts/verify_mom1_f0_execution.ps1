param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Python = "",
    [string]$ReleaseId = "tw_stock_data_2005_2014_r2",
    [string]$Output = "reports/mom1_f0_execution_readiness.json",
    [string]$ExpectedSha256 = "95751DC46196FB2837029D4AA64D0F0EFA225F9CDCDEB3C2AD617C1AF9DCD8DD"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
$pythonCandidates = @()
if ($Python) {
    $pythonCandidates += $Python
}
$pythonCandidates += (Join-Path $repo ".venv-repro/Scripts/python.exe")
$pythonCandidates += (Join-Path $repo ".venv/Scripts/python.exe")
$pythonExe = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonExe) {
    throw "MOM1 F0 diagnostic requires .venv-repro or .venv with pandas and pyarrow"
}

$outputPath = Join-Path $repo $Output
& $pythonExe (Join-Path $repo "scripts/diagnose_mom1_f0_execution.py") `
    --release-id $ReleaseId --output $outputPath
if ($LASTEXITCODE -ne 0) {
    throw "MOM1 F0 execution diagnostic failed"
}

$actual = (Get-FileHash -LiteralPath $outputPath -Algorithm SHA256).Hash
if ($actual -ne $ExpectedSha256) {
    throw "MOM1 F0 diagnostic SHA-256 mismatch: expected=$ExpectedSha256 actual=$actual"
}

[PSCustomObject]@{
    passed = $true
    data_release_id = $ReleaseId
    diagnostic_sha256 = $actual
    output = $outputPath
    f0_status = "blocked"
    performance_inspected = $false
} | ConvertTo-Json -Depth 3
