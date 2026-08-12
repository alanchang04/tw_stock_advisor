# Deployment-machine data release

Release: `tw_stock_data_2005_2014_r2`

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

- `tw_stock_data_2005_2014_r2.zip`
- `tw_stock_data_2005_2014_r2.zip.sha256`

## On the deployment machine

First update the code in a clean checkout:

```powershell
git fetch origin
git switch agent/swing-margin-research
git pull --ff-only origin agent/swing-margin-research
```

Verify the archive before extraction:

```powershell
$expected = (Get-Content .\tw_stock_data_2005_2014_r2.zip.sha256).Split()[0]
$actual = (Get-FileHash .\tw_stock_data_2005_2014_r2.zip -Algorithm SHA256).Hash
if ($actual -ne $expected) { throw "archive SHA-256 mismatch" }
```

Extract at the repository root, then verify every released file and component:

```powershell
tar.exe -x -f .\tw_stock_data_2005_2014_r2.zip -C .
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\verify_data_release.ps1
```

The verifier uses `.venv-repro` or `.venv` when available. On a fresh Windows
checkout with no Python installation, it automatically falls back to native
PowerShell SHA-256 verification.

Success requires `passed: true`, 15,134 verified files, collection SHA-256
`93F20503A24BEE8EAFD6F45E12B591B073A670D6265EF42DF5E0CE5C14C73109`,
and all nine component checks passing.

## Usage boundary

The release is approved for cross-machine identity checks, engine-correctness
tests, PIT signal counts, and disposition/universe filters. It is not approved
for backward holdout performance or parameter tuning because the corporate-
action execution ledger still has documented blockers. The machine-readable
details are in
`reports/data_releases/tw_stock_data_release_2005_2014_r2.json`.

## Reproduce the MOM1 signal-only diagnostic

This step requires `.venv-repro` or `.venv` with pandas and pyarrow. It verifies
the descriptor/component/input hashes again, builds only PIT universe and signal
counts, and refuses to run when the strategy contract files are dirty:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\verify_mom1_release_diagnostic.ps1
```

Expected diagnostic SHA-256:
`50DED9226307D363EA926F61584DF3AEFF2A2A9527F9AFBE7DA9EAE26C080A9F`.
The machine-readable result is `reports/mom1_release_diagnostic.json`; the
human-readable readiness summary is `reports/MOM1_RELEASE_READINESS.md`.
This command does not calculate or expose backward-holdout performance.

Reproduce the F0 portfolio-construction and execution-readiness audit as well:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\verify_mom1_f0_execution.ps1
```

Expected SHA-256:
`BF815DBF0264E49D1398447A7E3F2D48B7A6E7C57ED2AD4111CEA90CDAB6E260`.
The expected `f0_status` is currently `blocked`; that is a truthful gate status,
not a failed verifier. The report must also say `performance_inspected: false`.
