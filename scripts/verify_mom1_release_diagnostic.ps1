param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Python = "",
    [string]$ReleaseId = "tw_stock_data_2005_2014_r2",
    [string]$Output = "reports/mom1_release_diagnostic.json",
    [string]$ExpectedSha256 = "29CB56DA16F9D26417EDF630B68B8E868E5AECE5A9E97805AF014D617865211B"
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
    throw "MOM1 diagnostic requires .venv-repro or .venv with pandas and pyarrow"
}

$outputPath = Join-Path $repo $Output
& $pythonExe (Join-Path $repo "scripts/diagnose_mom1_release.py") `
    --release-id $ReleaseId --output $outputPath
if ($LASTEXITCODE -ne 0) {
    throw "MOM1 named-release diagnostic failed"
}

$actual = (Get-FileHash -LiteralPath $outputPath -Algorithm SHA256).Hash
if ($actual -ne $ExpectedSha256) {
    throw "MOM1 diagnostic SHA-256 mismatch: expected=$ExpectedSha256 actual=$actual"
}

[PSCustomObject]@{
    passed = $true
    data_release_id = $ReleaseId
    diagnostic_sha256 = $actual
    output = $outputPath
    performance_inspected = $false
} | ConvertTo-Json -Depth 3
