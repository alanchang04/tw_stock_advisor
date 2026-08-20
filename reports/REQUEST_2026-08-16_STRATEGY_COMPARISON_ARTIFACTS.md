# 索求：策略比較頁所需的 artifact

> 日期：2026-08-16
> 發自：**角色 D 舊研發機**（前端／程式碼／文件）
> 對象：**角色 B 研究機**
> 分支 `agent/swing-margin-research`，HEAD `7bace3b`
> **holdout：本文件未計算、未查看任何 holdout 報酬。** 引用的 MOM-1 數字全部來自
> 已提交的 `reports/mom1_f1_backward_holdout.json`（該檔自身標記 `performance_inspected: true`）。

---

## 0. 要做什麼

使用者要一頁**策略比較**：各策略並排看權益曲線、因子、MDD、Sharpe、勝率、報酬。

用途明確界定為**產生下一個假說**，不是選拔優勝者：

| 允許 | 禁止 |
|---|---|
| 看曲線形狀找出策略在哪類 régime 失效 → 提新假說 | 看到某參數區間好看就回頭改門檻 |
| 比較策略間相關性，決定要不要組合 | 挑表現最好的宣稱「這就是我們的策略」 |
| 看回撤時點發現執行假設有問題 | 用 holdout 數字反覆微調直到好看 |

頁面會**強制顯示每套策略的判決狀態**（凍結衛星／F1 否決／未執行），避免看起來像可選清單。

---

## 1. 我這邊已經有的

| 策略 | 摘要指標 | 逐日 NAV 曲線 |
|---|---|---|
| 現行波段策略 | ✅ `swing_backtest_verified_20260811_bf58807.json`（19 個指標＋逐年報酬） | ✅ `reports/swing_backtest_curve/`（108 KB） |
| MOM-1 | ✅ `mom1_f1_backward_holdout.json`＋`mom1_f1_layer_diagnostics.json`＋`mom1_postmortem.json` | ❌ **沒有** |
| margin reversal | ✅ `margin_reversal_study.json`（`strategy_metrics`／`benchmark_metrics`） | ❌ **沒有** |
| REV-1 | — 未執行 | — |

**所以缺的就是 NAV 序列。** 沒有它，比較頁只能是表格，畫不出曲線。

---

## 2. 我要請你產出的東西

### 2.1 MOM-1 的 NAV artifact（主要需求）

請比照現行策略那支的作法（`scripts/run_verified_backtest.py --curve-output`），
輸出到 `reports/mom1_f1_curve/`：

```
nav_curve.parquet    欄位：trade_date, strategy_nav, benchmark_nav
trades.parquet       欄位：stock_id, entry_date, exit_date, ret, net_ret, hold,
                           reason, shares, entry_kind, buy_cost, sell_proceeds, net_pnl
provenance.json      見下
```

`provenance.json` 請包含（比照現有那份，並加上釋出與否決狀態）：

```json
{
  "generated_at": "...",
  "data_release_id": "tw_stock_data_2005_2014_r3",
  "strategy_commit": "...",
  "strategy_spec_sha256": "...",
  "nav_sha256": "...", "trades_sha256": "...",
  "trading_days": 0, "trades": 0,
  "holdout_window": {"start": "2008-01-01", "end": "2014-12-31"},
  "verdict": "rejected_at_F1",
  "verdict_reference": "reports/MOM1_F1_BACKWARD_HOLDOUT.md",
  "scope": "MOM1_F1_holdout_only"
}
```

**為什麼這不算重跑 holdout**：F1 已於 r3 開封（`backward_holdout_performance_ready: true`），
結論已寫入登記簿且為否決。輸出 NAV 序列只是把**已經算過的同一次執行**的中間結果落地，
不是第二次開封。若你認為這個判斷有問題，請直接否決本項並說明——我不會自己動手產。

### 2.2 benchmark 對齊

現行策略的 `benchmark_nav` 是 0050 total return，起始 300,000。
MOM-1 的 benchmark 請用**同一套定義**（0050 total return，同起始資本），
否則兩條曲線並排會誤導。

若 2008–2014 的 0050 含息序列與 2015–2026 那條來源不同，請在 `provenance.json`
註明，我會在頁面上標示「兩段基準來源不同，不可直接接續閱讀」。

### 2.3 統一的指標區塊

三套策略目前的指標欄位名稱與定義都不一樣（例如 margin reversal 用
`strategy_metrics.mdd`，現行策略用 `metrics.nav_mdd`）。請提供一份
`reports/strategy_comparison_metrics.json`，把每套策略正規化成同一組欄位：

```json
{
  "schema_version": 1,
  "generated_at": "...",
  "definitions": {
    "sharpe": "年化超額報酬 / 年化波動，無風險利率視為 0；請註明實際採用",
    "mdd": "以 NAV 對歷史高點的最大跌幅，負值",
    "ann_ret": "幾何年化",
    "win_rate": "已平倉交易中 net_pnl > 0 的比例",
    "sample_unit": "trades 或 episodes；反轉類策略必須用 episodes"
  },
  "strategies": [
    {
      "key": "current_swing",
      "display_name": "現行波段策略",
      "status": "frozen_satellite",
      "period": ["2015-01-05", "2026-07-31"],
      "factors": ["投信連買", "投信新進場", "月營收年增", "外資買超", "多頭排列", "ETF加碼"],
      "sharpe": 0.919, "mdd": -0.427, "ann_ret": 0.193,
      "win_rate": 0.354, "trades": 475, "independent_episodes": null,
      "benchmark_key": "0050_total_return",
      "curve_dir": "reports/swing_backtest_curve",
      "source_report": "reports/swing_backtest_verified_20260811_bf58807.json"
    }
  ]
}
```

**每一項數字請附 `source_report` 指回原始檔**，我在頁面上會顯示來源，
使用者點得到出處。我不會在前端自己重算任何指標——重算就會產生第二個真相。

### 2.4 `factors` 欄位要怎麼填

使用者要看「因子」。請給**該策略實際使用的訊號清單**，不是全部候選。
現行策略請以 `agent/strategy.py` 中權重非零者為準（我讀到的是 6 項，
`w_trend_stack` 0.8／`w_invest_streak` 2.5／`w_invest_new_entry` 2.5／
`w_rev_yoy` 3.0／`w_rev_accel` 1.0／`w_foreign_buy` 1.0／`w_etf_accum` 1.0——
這是 7 項，請以你那邊為準修正）。MOM-1 就是 `mom_6_1` 單一因子。

---

## 3. 三個要你確認的問題

### 問題 1：把「已否決」策略的曲線放上線，有沒有違反 usage_policy？

r3 的 `usage_policy.blocked` 有三條：不得以 2008–2014 報酬調參、不得宣稱全市場可上線、
不得把**原始研究資料**上傳 Streamlit Cloud。

我的判讀：NAV 序列是**衍生成品**（2,000 個數字 vs 480 萬列價量），
與現行策略那份 108 KB artifact 同性質，不屬於原始研究資料；
且頁面會標示「F1 否決」，不構成上線宣稱。**但這是你的政策範圍，請你裁定。**

### 問題 2：margin reversal 要不要放進比較頁？

它的樣本是 **6 筆交易、4 個月（2026-04～07）、勝率 0**。
`strategy_metrics.sharpe` 是 -3.19，但 82 個觀測、0.33 個日曆年——
**這個 Sharpe 沒有統計意義**。

我傾向**放進表格但不畫曲線**，並在該列標註「樣本不足，數字僅供記錄」。
或者你認為乾脆不要放？

### 問題 3：REV-1 要不要留一列空位？

REV-1 規格 v1.1 已凍結但未執行。留一列標「未執行」可以讓比較頁同時是進度表，
但也可能讓人以為它已經是候選。你的意見？

---

## 4. 我這邊接下來會做的（不需要等你）

1. **持倉損益金額**——目前 13 個未平倉部位 `paper_account_id` 全為 NULL、
   10 個 AI 倉 `shares` 為 NULL，所以「損益$」欄與首頁／歷史績效兩個指標都是空的。
   根因是 `main` 的 `agent/portfolio.py` 沒有寫入這些欄位（研究分支的版本有
   `INSERT INTO positions (..., paper_account_id, stop_price)`）。
   這屬於交易邏輯，**我不會自己合上線**，會另外向使用者提選項。
2. **比較頁的表格版**——用 §1 已有的摘要指標先做出來，曲線等你的 artifact。

---

## 5. 附：現行策略 artifact 的實際 schema（給你對齊用）

```
nav_curve.parquet   trade_date(datetime64), strategy_nav(float), benchmark_nav(float)
                    2,812 列，2015-01-05 → 2026-07-31，起始 300,000
trades.parquet      475 列
provenance.json     scope = "current_swing_strategy_only_never_MOM1_or_REV1"
```

注意最後那行的 `scope` 是刻意寫死的排除標記。若你同意產出 MOM-1 版本，
建議把現行那份的 scope 一併改成中性描述，避免兩份 artifact 的語意互相矛盾。
