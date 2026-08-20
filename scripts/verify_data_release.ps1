param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Descriptor = "reports/data_releases/tw_stock_data_release_2005_2014_r2.json",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
$descriptorPath = Join-Path $repo $Descriptor
$release = Get-Content -Raw -Encoding utf8 $descriptorPath | ConvertFrom-Json
$transferPath = Join-Path $repo $release.transfer.manifest_path

$actualManifestHash = (Get-FileHash -LiteralPath $transferPath -Algorithm SHA256).Hash
if ($actualManifestHash -ne $release.transfer.manifest_sha256) {
    throw "transfer manifest SHA-256 mismatch"
}

$pythonCandidates = @()
if ($Python) {
    $pythonCandidates += $Python
}
$pythonCandidates += (Join-Path $repo ".venv-repro/Scripts/python.exe")
$pythonCandidates += (Join-Path $repo ".venv/Scripts/python.exe")
$python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

$transfer = Get-Content -Raw -Encoding utf8 $transferPath | ConvertFrom-Json
if ($transfer.collection_sha256 -ne $release.transfer.collection_sha256) {
    throw "transfer collection SHA-256 does not match the release descriptor"
}

if ($python) {
    & $python (Join-Path $repo "scripts/build_data_transfer_manifest.py") verify `
        --base $repo --manifest $transferPath
    if ($LASTEXITCODE -ne 0) {
        throw "release file verification failed"
    }
}
else {
    $missing = [System.Collections.Generic.List[string]]::new()
    $mismatched = [System.Collections.Generic.List[string]]::new()
    foreach ($file in $transfer.files) {
        $path = Join-Path $repo $file.path
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $missing.Add($file.path)
            continue
        }
        $item = Get-Item -LiteralPath $path
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if ($item.Length -ne $file.bytes -or $hash -ne $file.sha256) {
            $mismatched.Add($file.path)
        }
    }
    if ($missing.Count -gt 0 -or $mismatched.Count -gt 0) {
        throw "release verification failed: missing=$($missing.Count), mismatched=$($mismatched.Count)"
    }
    [PSCustomObject]@{
        passed = $true
        verifier = "powershell_sha256_fallback"
        checked_files = $transfer.file_count
        missing = 0
        mismatched = 0
        collection_sha256 = $transfer.collection_sha256
    } | ConvertTo-Json -Depth 3
}

$componentResults = foreach ($component in $release.components) {
    $snapshot = Join-Path $repo $component.snapshot_path
    $manifest = Join-Path $snapshot "manifest.json"
    $quality = Join-Path $snapshot "quality_report.json"
    $actualComponentManifestHash = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash
    $actualQualityHash = (Get-FileHash -LiteralPath $quality -Algorithm SHA256).Hash
    $componentManifest = Get-Content -Raw -Encoding utf8 $manifest | ConvertFrom-Json
    if ($actualComponentManifestHash -ne $component.manifest_sha256) {
        throw "$($component.component_id): manifest SHA-256 mismatch"
    }
    if ($actualQualityHash -ne $component.quality_sha256) {
        throw "$($component.component_id): quality SHA-256 mismatch"
    }
    if ($componentManifest.content_sha256 -ne $component.content_sha256) {
        throw "$($component.component_id): content SHA-256 mismatch"
    }
    [PSCustomObject]@{
        component_id = $component.component_id
        passed = $true
        content_sha256 = $component.content_sha256
    }
}

git -C $repo merge-base --is-ancestor $release.builder_contract.minimum_code_commit HEAD
if ($LASTEXITCODE -ne 0) {
    throw "repository HEAD does not contain the minimum release code commit"
}

[PSCustomObject]@{
    passed = $true
    data_release_id = $release.data_release_id
    verified_files = $release.transfer.file_count
    verified_bytes = $release.transfer.total_bytes
    collection_sha256 = $release.transfer.collection_sha256
    components = $componentResults
} | ConvertTo-Json -Depth 5
