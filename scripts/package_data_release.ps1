param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Descriptor = "reports/data_releases/tw_stock_data_release_2005_2014_r2.json",
    [string]$OutputPath = "data_release_bundles/tw_stock_data_2005_2014_r2.zip",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
$descriptorPath = Join-Path $repo $Descriptor
$release = Get-Content -Raw -Encoding utf8 $descriptorPath | ConvertFrom-Json
$transferPath = Join-Path $repo $release.transfer.manifest_path
$transfer = Get-Content -Raw -Encoding utf8 $transferPath | ConvertFrom-Json
$output = [System.IO.Path]::GetFullPath((Join-Path $repo $OutputPath))

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    (Join-Path $repo "scripts/verify_data_release.ps1") `
    -RepoRoot $repo -Descriptor $Descriptor
if ($LASTEXITCODE -ne 0) {
    throw "pre-package release verification failed"
}

if ((Test-Path -LiteralPath $output) -and -not $Force) {
    throw "archive already exists: $output (use -Force to replace it)"
}
[System.IO.Directory]::CreateDirectory((Split-Path -Parent $output)) | Out-Null

$controlFiles = @(
    $Descriptor,
    $release.transfer.manifest_path,
    "docs/DEPLOYMENT_MACHINE_DATA_RELEASE.md",
    "scripts/build_data_transfer_manifest.py",
    "scripts/verify_data_release.ps1"
)
$entries = @($transfer.files.path) + $controlFiles
$entries = $entries | Sort-Object -Unique
$listPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    "tw-stock-release-" + [guid]::NewGuid().ToString("N") + ".txt"
)
try {
    [System.IO.File]::WriteAllLines($listPath, $entries, [System.Text.UTF8Encoding]::new($false))
    Push-Location $repo
    try {
        if ((Test-Path -LiteralPath $output) -and $Force) {
            Remove-Item -LiteralPath $output -Force
        }
        & tar.exe -a -c -f $output -T $listPath
        if ($LASTEXITCODE -ne 0) {
            throw "tar archive creation failed"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    Remove-Item -LiteralPath $listPath -Force -ErrorAction SilentlyContinue
}

$archiveHash = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash
$hashPath = "$output.sha256"
Set-Content -LiteralPath $hashPath -Encoding ascii -NoNewline `
    -Value "$archiveHash  $([System.IO.Path]::GetFileName($output))`n"
[PSCustomObject]@{
    archive = $output
    bytes = (Get-Item -LiteralPath $output).Length
    sha256 = $archiveHash
    checksum_file = $hashPath
    data_release_id = $release.data_release_id
} | ConvertTo-Json -Depth 3
