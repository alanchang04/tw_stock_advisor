# Q 前一日 Watchlist＋次日 Stop-Buy 研究

> 僅使用 development；validation 與 holdout 均未讀取。正式策略未變更。

## 可執行時序

1. T 日收盤後，以截至 T 日的資料建立 watchlist 與箱頂觸發價。
2. T+1 預掛 buy-stop；最高價未觸及則取消，跳空越過則以開盤價加滑價成交。
3. 初始停損只用 T 日已知 ADR，不使用 T+1 最低價決定停損距離。
4. 日 K 無法知道先高後低或先低後高，因此同時報告 conservative／relaxed 邊界。

## 固定規格

```json
{
  "prior_move_days": 60,
  "base_days": 20,
  "prior_move_min": 0.3,
  "rs_quantile": 0.9,
  "adr_days": 20,
  "adr_min": 0.04,
  "base_depth_max": 0.25,
  "breakout_volume_multiple": 1.5,
  "close_top_fraction": 0.33,
  "near_high_fraction": 0.8,
  "watch_distance_max": 0.05,
  "trail_start_day": 5,
  "partial_day": 5,
  "initial_risk_cap_adr": 1.0
}
```

訊號日期：911；實際結果：

| 版本 | 年化 | Sharpe | MDD | 交易 | 勝率 | 同日停損 | Day3 | Day5 | Day10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q_stop__current | 4.19% | 0.30 | -44.52% | 226 | 25.7% | 0 | -0.17% | -0.03% | 1.53% |
| q_stop__q_full_10ma__conservative | -6.28% | -0.54 | -44.97% | 394 | 22.8% | 111 | -0.50% | -0.53% | 0.59% |
| q_stop__q_full_10ma__relaxed | -2.25% | -0.15 | -33.42% | 374 | 25.7% | 38 | -0.45% | -0.52% | 0.60% |
| q_stop__q_half_day5_10ma__conservative | -7.87% | -0.91 | -43.66% | 395 | 28.4% | 112 | -0.50% | -0.53% | 0.58% |
| q_stop__q_half_day5_10ma__relaxed | -4.38% | -0.47 | -33.26% | 374 | 31.8% | 38 | -0.45% | -0.52% | 0.60% |
| q_stop__q_half_day5_20ma__conservative | -9.22% | -1.25 | -44.20% | 415 | 27.2% | 117 | -0.53% | -0.50% | 0.66% |
| q_stop__q_half_day5_20ma__relaxed | -5.96% | -0.77 | -33.75% | 399 | 31.3% | 42 | -0.44% | -0.47% | 0.71% |

## 對照

- 現行／現行：年化 13.55%、Sharpe 0.81、MDD -26.64%。
- 收盤確認後隔日追進的 Q／現行：年化 6.21%、Sharpe 0.54。

## 結論

- **預掛成交只解決部分追價。** 相較收盤確認後隔日追進，Day3/Day5 約由 -1.00%/-0.99% 改善至 -0.45%/-0.52%，但仍未轉正。
- **Q進場配Q出場仍失敗。** 即使採對交易最有利的 relaxed 日內順序，最佳的全倉10MA仍為負年化與負Sharpe；保守邊界更差。
- **日內順序不是翻盤關鍵。** conservative 與 relaxed 的績效幅度不同，但方向一致為負。
- **暫不取得5分鐘資料。** 目前不存在正的日線上界可供精煉；5分鐘資料可能改善選擇，但沒有證據顯示足以跨越與現行基準的巨大差距。
- **對現行策略的啟示。** 下一個合理實驗不是繼續調Q參數，而是保留現行的營收/法人品質選股，改用『前一日接近緊密箱頂→次日預掛突破』處理進場時機，並繼續使用現行出場。

## 自動判決

```json
{
  "paired_q_variants_beating_current_baseline": [],
  "bounds": {
    "conservative": {
      "best_variant": "q_stop__q_full_10ma__conservative",
      "annual_return": -0.06278579378856686,
      "sharpe": -0.536632969086191,
      "mdd": -0.449655128662674,
      "same_day_stops": 111
    },
    "relaxed": {
      "best_variant": "q_stop__q_full_10ma__relaxed",
      "annual_return": -0.02252020259623777,
      "sharpe": -0.14866371069396675,
      "mdd": -0.334235418194341,
      "same_day_stops": 38
    }
  },
  "worth_acquiring_5min_data": false,
  "validation_run": "skipped_development_gate",
  "formal_defaults_changed": false
}
```
