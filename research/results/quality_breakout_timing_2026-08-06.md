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
| C0_formal_5d | 13.87% | 0.83 | -25.59% | 0.54 | 236 | 41.5% | 41.3 | 68.3% | 3.95 | 28.4% |
| C1_daily_next_open | 12.81% | 0.78 | -25.21% | 0.51 | 280 | 40.4% | 39.4 | 77.5% | 4.59 | 28.9% |
| H1_quality_stop_buy | 15.24% | 0.90 | -26.05% | 0.59 | 246 | 41.1% | 43.6 | 75.1% | 4.00 | 32.1% |
| H2_quality_tight_stop_buy | 15.41% | 0.92 | -19.50% | 0.79 | 249 | 41.8% | 40.7 | 71.0% | 3.97 | 31.7% |
| H3_quality_tight_dryup | 12.03% | 0.79 | -23.93% | 0.50 | 236 | 37.7% | 39.7 | 65.8% | 3.88 | 37.7% |

## 無條件進場路徑（提前停損者仍繼續追蹤原股票）

| 版本 | Day3 | Day5 | Day10 | Day20 | Day20>0 佔比 | MFE | MAE |
|---|---:|---:|---:|---:|---:|---:|---:|
| C0_formal_5d | 0.60% | 0.80% | 1.97% | 2.18% | 49.58% | 19.60% | -6.15% |
| C1_daily_next_open | 0.29% | 0.82% | 2.27% | 2.08% | 53.57% | 17.87% | -6.11% |
| H1_quality_stop_buy | 0.19% | 0.81% | 1.62% | 1.95% | 51.22% | 20.47% | -6.41% |
| H2_quality_tight_stop_buy | 0.23% | 0.72% | 1.48% | 2.72% | 54.22% | 19.40% | -6.29% |
| H3_quality_tight_dryup | 0.07% | 0.20% | 0.80% | 1.06% | 47.88% | 18.70% | -6.70% |

## 成交品質與右尾集中度

| 版本 | 未觸價取消率 | 跳空成交率 | Top1 | Top5 | Top10 | 移除最佳5筆後淨損益 |
|---|---:|---:|---:|---:|---:|---:|
| C0_formal_5d | — | — | 15.2% | 30.7% | 44.4% | 100,184 |
| C1_daily_next_open | — | — | 5.4% | 23.2% | 38.0% | 128,464 |
| H1_quality_stop_buy | 43.1% | 45.1% | 8.5% | 27.2% | 44.5% | 166,198 |
| H2_quality_tight_stop_buy | 52.8% | 41.4% | 5.3% | 24.1% | 39.8% | 205,488 |
| H3_quality_tight_dryup | 56.5% | 36.0% | 9.5% | 34.1% | 50.4% | 45,369 |

## 分年報酬（含 0050 同成本口徑）

| 版本 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 |
|---|---|---|---|---|---|---|
| C0_formal_5d | 16.7% | 3.6% | 48.7% | -2.9% | 12.9% | 10.4% |
| C1_daily_next_open | 3.6% | 16.0% | 46.0% | -15.9% | 22.8% | 13.4% |
| H1_quality_stop_buy | 10.4% | 16.1% | 46.7% | -17.6% | 7.8% | 40.0% |
| H2_quality_tight_stop_buy | 23.0% | 7.2% | 18.2% | -7.2% | 4.7% | 55.7% |
| H3_quality_tight_dryup | 6.9% | 3.3% | 22.2% | -12.1% | 9.8% | 51.4% |
| **0050 買進持有** | -6.2% | 19.6% | 18.1% | -4.9% | 33.5% | 30.2% |

0050 同期：總報酬 119.99%、年化 14.07%、Sharpe 0.89、MDD -28.22%。

## 預先登記判決（F0~F5）

```json
{
  "baseline_reproduced": {
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
    "annual_return": -0.010612987842943511,
    "sharpe": -0.05350860940170521,
    "mdd": 0.0037920249090197533
  },
  "per_variant": {
    "H1_quality_stop_buy": {
      "F1_beats_c0_on_return_and_sharpe": true,
      "F2_mdd_not_worse_by_3pp": true,
      "F3_positive_without_best_5": true,
      "F4_not_concentrated": false,
      "max_year_profit_share": 0.43916541126199654,
      "max_industry_profit_share": 0.19964523852935578,
      "passes_all": false
    },
    "H2_quality_tight_stop_buy": {
      "F1_beats_c0_on_return_and_sharpe": true,
      "F2_mdd_not_worse_by_3pp": true,
      "F3_positive_without_best_5": true,
      "F4_not_concentrated": false,
      "max_year_profit_share": 0.6277832762296042,
      "max_industry_profit_share": 0.27025365228554266,
      "passes_all": false
    },
    "H3_quality_tight_dryup": {
      "F1_beats_c0_on_return_and_sharpe": false,
      "F2_mdd_not_worse_by_3pp": true,
      "F3_positive_without_best_5": true,
      "F4_not_concentrated": false,
      "max_year_profit_share": 0.6923235111868714,
      "max_industry_profit_share": 0.2798298027288009,
      "passes_all": false
    }
  },
  "passing_variants": [],
  "F5_ladder_sharpes": {
    "H1_quality_stop_buy": 0.8957312224536638,
    "H2_quality_tight_stop_buy": 0.9185286248624219,
    "H3_quality_tight_dryup": 0.7877662088605211
  },
  "F5_monotone": false,
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
