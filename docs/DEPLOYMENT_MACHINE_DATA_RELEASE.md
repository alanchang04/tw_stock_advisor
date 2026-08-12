# Deployment-machine data release

Release: `tw_stock_data_2005_2014_r1`

This bundle is a reproducible research mirror for the deployment machine. It is
not a Streamlit Cloud runtime dependency and must not be uploaded to Streamlit.
The web application continues to deploy code from GitHub and use PostgreSQL via
`DATABASE_URL`.

## On the Data Authority machine

From the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\package_data_release.ps1
```

Transfer both files in `data_release_bundles/` to the deployment machine:

- `tw_stock_data_2005_2014_r1.zip`
- `tw_stock_data_2005_2014_r1.zip.sha256`

## On the deployment machine

First update the code in a clean checkout:

```powershell
git fetch origin
git switch agent/swing-margin-research
git pull --ff-only origin agent/swing-margin-research
```

Verify the archive before extraction:

```powershell
$expected = (Get-Content .\tw_stock_data_2005_2014_r1.zip.sha256).Split()[0]
$actual = (Get-FileHash .\tw_stock_data_2005_2014_r1.zip -Algorithm SHA256).Hash
if ($actual -ne $expected) { throw "archive SHA-256 mismatch" }
```

Extract at the repository root, then verify every released file and component:

```powershell
tar.exe -x -f .\tw_stock_data_2005_2014_r1.zip -C .
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\verify_data_release.ps1
```

Success requires `passed: true`, 12,661 verified files, collection SHA-256
`44EAF107359CA2754A7407A062E4ABD1A1F61A16298C6816E11994EEF604CD1E`,
and all eight component checks passing.

## Usage boundary

The release is approved for cross-machine identity checks, engine-correctness
tests, PIT signal counts, and disposition/universe filters. It is not approved
for backward holdout performance or parameter tuning because the corporate-
action execution ledger still has documented blockers. The machine-readable
details are in
`reports/data_releases/tw_stock_data_release_2005_2014_r1.json`.
