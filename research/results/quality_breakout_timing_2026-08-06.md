# P3-9 品質選股 × 可執行突破時機

> development only（2015-01~2020-12）。validation 未讀取（2026-08 額度已由 P3-6 用掉）、
> holdout 未讀取。正式 `STRATEGY` 未變更。所有門檻在看到結果前已寫進 `research/EXPERIMENTS.md`。

## 這一輪只動進場

停損固定 8%、出場用現行 `decide_exit`、market filter、sector cap、宇宙濾網與成本口徑
全部維持正式值，五列共用同一個 portfolio engine 與同一份品質排名。

## 執行環境（換環境數字就不同，比對前先核對）

```json
{
  "python": "3.12.9",
  "platform": "Windows-11-10.0.26200-SP0",
  "pandas": "2.3.3",
  "numpy": "2.5.1",
  "scipy": "1.18.0",
  "parquet_dir": "C:\\Users\\alanchang\\Desktop\\taiwan_stock_advisor\\data\\research",
  "data": {
    "price_rows": 4840274,
    "price_stocks": 2140,
    "price_days": 2812,
    "price_first": "2015-01-05",
    "price_last": "2026-07-31",
    "median_stocks_per_day": 1716.0,
    "dividend_events": 16919
  },
  "note": "回測數字只在這組環境下可複製；pandas 版本會改變選股結果，比對他人數字前先核對本區塊（見 requirements.txt 的鎖版註解）"
}
```

## F0 基準重現

```json
{
  "purpose": "universe_markets=None 時必須與改動前完全相同（no-op 回歸檢查）",
  "expected": {
    "annual_return": 13.87,
    "sharpe": 0.83,
    "mdd": -25.59,
    "trades": 236
  },
  "observed": {
    "annual_return": 13.87,
    "sharpe": 0.83,
    "mdd": -25.59,
    "trades": 236
  },
  "checks": {
    "annual_return": true,
    "sharpe": true,
    "mdd": true,
    "trades": true
  },
  "passed": true
}
```

## 主結果

| 版本 | 年化 | Sharpe | MDD | Calmar | 交易 | 勝率 | 平均持有 | 曝險 | 週轉/年 | 停損率 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| C0_formal_5d | 18.36% | 1.13 | -15.15% | 1.21 | 225 | 42.7% | 43.5 | 68.6% | 3.66 | 24.4% |
| C1_daily_next_open | 17.95% | 1.10 | -14.41% | 1.25 | 268 | 41.4% | 41.2 | 77.5% | 4.33 | 24.6% |
| H1_quality_stop_buy | 18.37% | 1.13 | -17.08% | 1.08 | 236 | 45.3% | 45.4 | 75.0% | 3.84 | 28.8% |
| H2_quality_tight_stop_buy | 14.33% | 0.96 | -16.05% | 0.89 | 227 | 45.4% | 44.4 | 70.6% | 3.66 | 26.4% |
| H3_quality_tight_dryup | 10.30% | 0.76 | -18.79% | 0.55 | 214 | 39.3% | 43.7 | 65.5% | 3.50 | 30.8% |

## 無條件進場路徑（提前停損者仍繼續追蹤原股票）

| 版本 | Day3 | Day5 | Day10 | Day20 | Day20>0 佔比 | MFE | MAE |
|---|---:|---:|---:|---:|---:|---:|---:|
| C0_formal_5d | 0.49% | 0.68% | 2.14% | 2.42% | 52.44% | 19.96% | -5.67% |
| C1_daily_next_open | 0.12% | 0.42% | 1.54% | 1.66% | 52.24% | 17.82% | -5.73% |
| H1_quality_stop_buy | 0.36% | 0.51% | 1.35% | 1.55% | 51.69% | 19.98% | -6.27% |
| H2_quality_tight_stop_buy | 0.18% | 0.58% | 1.37% | 2.17% | 55.07% | 18.24% | -6.06% |
| H3_quality_tight_dryup | -0.35% | -0.03% | 0.60% | 0.71% | 49.07% | 16.85% | -6.33% |

## 成交品質與右尾集中度

| 版本 | 未觸價取消率 | 跳空成交率 | Top1 | Top5 | Top10 | 移除最佳5筆後淨損益 |
|---|---:|---:|---:|---:|---:|---:|
| C0_formal_5d | — | — | 13.3% | 29.5% | 43.1% | 247,110 |
| C1_daily_next_open | — | — | 9.4% | 26.8% | 43.6% | 254,876 |
| H1_quality_stop_buy | 45.5% | 47.0% | 8.8% | 26.2% | 41.3% | 272,362 |
| H2_quality_tight_stop_buy | 55.2% | 40.5% | 7.1% | 27.2% | 42.7% | 170,031 |
| H3_quality_tight_dryup | 57.4% | 36.4% | 9.0% | 34.3% | 51.6% | 32,884 |

## 分年報酬（含 0050 同成本口徑）

| 版本 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 |
|---|---|---|---|---|---|---|
| C0_formal_5d | 16.6% | 3.6% | 48.7% | 10.4% | 13.7% | 21.7% |
| C1_daily_next_open | 3.1% | 16.1% | 44.4% | -6.6% | 18.0% | 41.2% |
| H1_quality_stop_buy | 15.8% | 11.3% | 49.0% | -13.0% | 26.0% | 30.5% |
| H2_quality_tight_stop_buy | 19.2% | 13.6% | 18.8% | -5.6% | 16.9% | 25.6% |
| H3_quality_tight_dryup | 6.9% | 6.6% | 24.8% | -5.0% | 5.5% | 26.1% |
| **0050 買進持有** | -6.2% | 19.6% | 18.1% | -4.9% | 33.5% | 30.2% |

0050 同期：總報酬 119.99%、年化 14.07%、Sharpe 0.89、MDD -28.22%。

## 預先登記判決（F0~F5）

```json
{
  "baseline_reproduced": {
    "purpose": "universe_markets=None 時必須與改動前完全相同（no-op 回歸檢查）",
    "expected": {
      "annual_return": 13.87,
      "sharpe": 0.83,
      "mdd": -25.59,
      "trades": 236
    },
    "observed": {
      "annual_return": 13.87,
      "sharpe": 0.83,
      "mdd": -25.59,
      "trades": 236
    },
    "checks": {
      "annual_return": true,
      "sharpe": true,
      "mdd": true,
      "trades": true
    },
    "passed": true
  },
  "cadence_effect_c1_minus_c0": {
    "annual_return": -0.0040606423244891054,
    "sharpe": -0.030459934142466505,
    "mdd": 0.0074080666341081736
  },
  "per_variant": {
    "H1_quality_stop_buy": {
      "F1_beats_c0_on_return_and_sharpe": false,
      "F2_mdd_not_worse_by_3pp": true,
      "F3_positive_without_best_5": true,
      "F4_not_concentrated": true,
      "max_year_profit_share": 0.35701745782122385,
      "max_industry_profit_share": 0.2450442068032504,
      "passes_all": false
    },
    "H2_quality_tight_stop_buy": {
      "F1_beats_c0_on_return_and_sharpe": false,
      "F2_mdd_not_worse_by_3pp": true,
      "F3_positive_without_best_5": true,
      "F4_not_concentrated": false,
      "max_year_profit_share": 0.4452230871669904,
      "max_industry_profit_share": 0.19685885411551904,
      "passes_all": false
    },
    "H3_quality_tight_dryup": {
      "F1_beats_c0_on_return_and_sharpe": false,
      "F2_mdd_not_worse_by_3pp": false,
      "F3_positive_without_best_5": true,
      "F4_not_concentrated": false,
      "max_year_profit_share": 0.5214645578303797,
      "max_industry_profit_share": 0.33304008671975904,
      "passes_all": false
    }
  },
  "passing_variants": [],
  "F5_ladder_sharpes": {
    "H1_quality_stop_buy": 1.1260198523538538,
    "H2_quality_tight_stop_buy": 0.9591031207560121,
    "H3_quality_tight_dryup": 0.7564138146222065
  },
  "F5_monotone": true,
  "F5_single_peak_warning": false,
  "thresholds_frozen": {
    "max_distance": 0.03,
    "tightness_max": 0.75,
    "dryup_days_vs_box_days": [
      5,
      20
    ],
    "note": "看到結果後不得調整，也不得掃鄰近值（§4.3 鄰居檢驗 / P3-5）"
  },
  "validation_touched": false,
  "holdout_touched": false,
  "formal_defaults_changed": false
}
```
