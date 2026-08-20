# 交接包內容

## 起點

- `CLAUDE_START_HERE.md`：完整研究結論、限制、下一輪預先規格。

## 核心程式

- `agent/`：目前策略、回測、paper ledger、daily runner。
- `margin_reversal/`：獨立融資反轉策略。
- `research/`：資料切分、統計研究、Qullamaggie 模組與結果。
- `scripts/`：回測、事件研究與新研究 runner。
- `tests/`：完整測試。
- `database/migrations/29_swing_risk_experiments.sql`：P3-6 schema。

## 文件／報表

- `docs/SPEC_QUANT_UPGRADE.md`
- `docs/SPEC_SWING_RISK_AND_MARGIN_RESEARCH.md`
- `docs/TRADING_LOGIC.md`
- `reports/margin_reversal_study.json`
- `reports/margin_reversal_study.md`
- `reports/margin_reversal_path_audit.json`
- `reports/swing_backtest_verified.json`

## 未包含

- `.env` 或任何 secret。
- `data/` parquet／行情原始資料。
- DB dump、log、cache、虛擬環境、Git 歷史。
- `.vscode/settings.json`。

## 驗證

建立交接包前完整測試：`461 passed, 55 skipped, 1 warning`。
