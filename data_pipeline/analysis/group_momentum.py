"""
data_pipeline/analysis/group_momentum.py

細分族群輪動熱度（記憶體/AI伺服器/散熱…，含龍頭股加權）。

與 sector_momentum（官方 54 大類）的差別：
  - 用 stock_groups / stock_group_members（migration 09，手工精選 + 龍頭標記）
  - 龍頭權重高（30%）：龍頭先動是輪動起點
  - 即時計算回傳 DataFrame，不落地（成員少、速度快），供 app 頁面與每日彙整使用

熱度分 = norm(平均漲跌)×0.4 + norm(上漲比例)×0.2 + norm(龍頭漲跌)×0.3 + norm(法人買超)×0.1
"""
from __future__ import annotations
from datetime import date

import pandas as pd
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from database.connection import get_session


def _normalize(s: pd.Series) -> pd.Series:
    rng = s.max() - s.min()
    return (s - s.min()) / rng if rng else pd.Series(0.5, index=s.index)


def latest_trade_date() -> date | None:
    with get_session() as s:
        return s.execute(text("SELECT MAX(trade_date) FROM daily_prices")).scalar()


def calc_group_momentum(target_date: date = None) -> pd.DataFrame:
    """
    回傳各族群當日熱度 DataFrame：
      group_code, group_name, avg_change_pct, rising, total, rising_ratio,
      leader_change_pct, leader_names, inst_net_lots, momentum_score
    依 momentum_score 降冪。無資料回傳空 DataFrame。
    """
    if target_date is None:
        target_date = latest_trade_date()
    if target_date is None:
        return pd.DataFrame()

    with get_session() as s:
        rows = s.execute(text("""
            SELECT g.group_code, g.group_name,
                   m.stock_id, st.stock_name, m.is_leader,
                   p.change_pct,
                   COALESCE(i.total_net, 0) AS inst_net
            FROM stock_group_members m
            JOIN stock_groups g  ON g.group_code = m.group_code
            JOIN stocks st       ON st.stock_id = m.stock_id
            JOIN daily_prices p  ON p.stock_id = m.stock_id AND p.trade_date = :dt
            LEFT JOIN institutional_trading i
                   ON i.stock_id = m.stock_id AND i.trade_date = :dt
            WHERE p.change_pct IS NOT NULL
        """), {"dt": target_date}).fetchall()

    if not rows:
        logger.warning(f"group_momentum: {target_date} 無資料")
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[
        "group_code", "group_name", "stock_id", "stock_name",
        "is_leader", "change_pct", "inst_net",
    ])
    df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce").astype(float)
    df["inst_net"]   = pd.to_numeric(df["inst_net"],   errors="coerce").astype(float)

    leaders = df[df["is_leader"]]
    agg = df.groupby(["group_code", "group_name"]).agg(
        avg_change_pct=("change_pct", "mean"),
        rising=("change_pct", lambda x: int((x > 0).sum())),
        total=("stock_id", "count"),
        inst_net_sum=("inst_net", "sum"),
    ).reset_index()

    lead_agg = leaders.groupby("group_code").agg(
        leader_change_pct=("change_pct", "mean"),
        leader_names=("stock_name", lambda x: "、".join(x)),
    ).reset_index()
    agg = agg.merge(lead_agg, on="group_code", how="left")
    agg["leader_change_pct"] = agg["leader_change_pct"].fillna(agg["avg_change_pct"])
    agg["leader_names"] = agg["leader_names"].fillna("—")

    agg["rising_ratio"] = agg["rising"] / agg["total"].clip(lower=1)
    # 三大法人單位是股 → ÷1000 轉張
    agg["inst_net_lots"] = (agg["inst_net_sum"] / 1000).round().astype(int)

    agg["momentum_score"] = (
        _normalize(agg["avg_change_pct"])    * 0.4
        + _normalize(agg["rising_ratio"])    * 0.2
        + _normalize(agg["leader_change_pct"]) * 0.3
        + _normalize(agg["inst_net_sum"])    * 0.1
    ).round(4)

    agg["trade_date"] = target_date
    return agg.drop(columns=["inst_net_sum"]) \
              .sort_values("momentum_score", ascending=False).reset_index(drop=True)


def group_members_detail(group_code: str, target_date: date = None) -> pd.DataFrame:
    """某族群成員當日明細：代號/名稱/龍頭/漲跌%/收盤/成交量/近5日法人(張)/RSI。"""
    if target_date is None:
        target_date = latest_trade_date()
    if target_date is None:
        return pd.DataFrame()

    with get_session() as s:
        rows = s.execute(text("""
            SELECT m.stock_id, st.stock_name, m.is_leader,
                   p.change_pct, p.close, p.volume,
                   t.rsi14,
                   COALESCE(i5.net5, 0) / 1000 AS inst_5d_lots
            FROM stock_group_members m
            JOIN stocks st      ON st.stock_id = m.stock_id
            JOIN daily_prices p ON p.stock_id = m.stock_id AND p.trade_date = :dt
            LEFT JOIN technical_indicators t
                   ON t.stock_id = m.stock_id AND t.trade_date = :dt
            LEFT JOIN (
                SELECT stock_id, SUM(total_net) AS net5
                FROM institutional_trading
                WHERE trade_date > :dt - INTERVAL '9 days' AND trade_date <= :dt
                GROUP BY stock_id
            ) i5 ON i5.stock_id = m.stock_id
            WHERE m.group_code = :gc
            ORDER BY p.change_pct DESC NULLS LAST
        """), {"gc": group_code, "dt": target_date}).fetchall()

    return pd.DataFrame(rows, columns=[
        "代號", "名稱", "龍頭", "漲跌%", "收盤", "成交量", "RSI", "近5日法人(張)",
    ])


def rotation_alerts(momentum_df: pd.DataFrame,
                    leader_min_change: float = 2.0,
                    min_rising_ratio: float = 0.6) -> list[str]:
    """輪動提示：龍頭當日漲 >2% 且族群上漲比例 >60%。"""
    alerts = []
    for _, r in momentum_df.iterrows():
        if r["leader_change_pct"] >= leader_min_change and r["rising_ratio"] >= min_rising_ratio:
            alerts.append(
                f"🔥 {r['group_name']} 輪動中：龍頭 {r['leader_names']} "
                f"{r['leader_change_pct']:+.1f}%，{r['rising']}/{r['total']} 家上漲"
            )
    return alerts


if __name__ == "__main__":
    df = calc_group_momentum()
    print(df.to_string(index=False))
    for a in rotation_alerts(df):
        print(a)
