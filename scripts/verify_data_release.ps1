param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Descriptor = "reports/data_releases/tw_stock_data_release_2005_2014_r1.json"
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

$python = Join-Path $repo ".venv-repro/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = Join-Path $repo ".venv/Scripts/python.exe"
}
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

& $python (Join-Path $repo "scripts/build_data_transfer_manifest.py") verify `
    --base $repo --manifest $transferPath
if ($LASTEXITCODE -ne 0) {
    throw "release file verification failed"
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
