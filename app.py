"""
app.py — 台股顧問系統 Streamlit 網頁介面

啟動方式：
    streamlit run app.py
或雙擊 run_app.bat
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
from sqlalchemy import text

from database.connection import get_session
from agent.strategy import decide_exit, STRATEGY

# ══════════════════════════════════════════════════════════════════
#  頁面設定
# ══════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="台股顧問系統",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.sidebar.title("📈 台股顧問")
page = st.sidebar.radio(
    "導覽",
    ["📊 首頁", "📦 持倉追蹤", "🏦 法人動向", "📉 個股走勢", "🔄 歷史績效", "📰 市場情報", "🧠 聰明資金"],
)
st.sidebar.markdown("---")

# 立即更新按鈕（觸發 GitHub Actions，雲端本機都能用）
if st.sidebar.button("🔄 立即更新資料", help="觸發 GitHub Actions 跑完整 pipeline，約 3-5 分鐘後資料生效"):
    import requests as _req
    _gh_pat = st.secrets.get("GITHUB_PAT", "")
    if not _gh_pat:
        st.sidebar.error("未設定 GITHUB_PAT，請在 Streamlit Secrets 加入後重試")
    else:
        _resp = _req.post(
            "https://api.github.com/repos/alanchang04/tw_stock_advisor/actions/workflows/daily_update.yml/dispatches",
            headers={
                "Authorization": f"Bearer {_gh_pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": "main"},
            timeout=10,
        )
        if _resp.status_code == 204:
            st.sidebar.success("✅ 已送出更新請求！約 3-5 分鐘後資料生效，請屆時重新整理頁面")
        else:
            st.sidebar.error(f"❌ 觸發失敗（{_resp.status_code}）：{_resp.text[:200]}")

st.sidebar.caption(f"今日：{date.today()}")


# ══════════════════════════════════════════════════════════════════
#  共用工具
# ══════════════════════════════════════════════════════════════════
def _clean_val(v):
    """把 None / NaN / 'None' / 'nan' / 空字串 統一轉成 None，否則回傳去空白字串。"""
    if v is None:
        return None
    try:
        if pd.isna(v):          # 處理 NaN / NaT
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if s in ("", "None", "nan", "NaN", "NaT"):
        return None
    return s


# ══════════════════════════════════════════════════════════════════
#  共用資料查詢（快取 5 分鐘）
# ══════════════════════════════════════════════════════════════════
@st.cache_data(ttl=300)
def db_status() -> dict:
    try:
        with get_session() as s:
            last  = s.execute(text("SELECT MAX(trade_date) FROM daily_prices")).scalar()
            sc    = s.execute(text("SELECT COUNT(*) FROM stocks WHERE is_active=true")).scalar()
            pos_c = s.execute(text("SELECT COUNT(*) FROM positions WHERE status='open'")).scalar()
        return {"ok": True, "last_date": last, "stocks": sc, "open_pos": pos_c}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@st.cache_data(ttl=60)
def load_open_positions() -> list[dict]:
    today = date.today()
    result = []
    with get_session() as s:
        rows = s.execute(text("""
            SELECT p.stock_id, st.stock_name, p.entry_date, p.entry_price, p.peak_price
            FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
            WHERE p.status = 'open' ORDER BY p.entry_date
        """)).fetchall()

        for r in rows:
            sid, name, entry_date, entry_price, peak_price = r
            entry_price = float(entry_price)
            peak_price  = float(peak_price) if peak_price else entry_price

            hist_rows = s.execute(text("""
                SELECT p.trade_date, p.open, p.high, p.low, p.close, p.volume,
                       t.ma5, t.ma20, t.macd_hist, t.k_value, t.d_value
                FROM daily_prices p
                LEFT JOIN technical_indicators t
                    ON t.stock_id = p.stock_id AND t.trade_date = p.trade_date
                WHERE p.stock_id = :sid AND p.trade_date <= :d AND p.close > 0
                ORDER BY p.trade_date DESC LIMIT 40
            """), {"sid": sid, "d": today}).fetchall()

            if not hist_rows:
                continue

            history = [dict(
                trade_date=h[0],
                open=float(h[1] or 0), high=float(h[2] or 0),
                low=float(h[3] or 0),  close=float(h[4] or 0),
                volume=float(h[5] or 0),
                ma5=float(h[6]) if h[6] is not None else None,
                ma20=float(h[7]) if h[7] is not None else None,
                macd_hist=float(h[8]) if h[8] is not None else None,
                k_value=float(h[9]) if h[9] is not None else None,
                d_value=float(h[10]) if h[10] is not None else None,
            ) for h in reversed(hist_rows)]

            today_r = history[-1]
            prev_r  = history[-2] if len(history) >= 2 else {}
            close   = today_r["close"]
            ma5     = today_r["ma5"]
            ma20    = today_r["ma20"]
            peak    = max(peak_price, close)

            past    = history[-(21):-1]
            avg_vol = sum(r["volume"] for r in past) / len(past) if past else None

            extra = dict(
                k=today_r["k_value"], d=today_r["d_value"],
                k_prev=prev_r.get("k_value"), d_prev=prev_r.get("d_value"),
                macd_hist=today_r["macd_hist"], macd_hist_prev=prev_r.get("macd_hist"),
                open=today_r["open"], high=today_r["high"],
                low=today_r["low"], volume=today_r["volume"], avg_volume=avg_vol,
            )

            hold = s.execute(text("""
                SELECT COUNT(DISTINCT trade_date) FROM daily_prices
                WHERE stock_id=:sid AND trade_date>:e AND trade_date<=:d
            """), {"sid": sid, "e": entry_date, "d": today}).scalar() or 0

            should_exit, reason = decide_exit(
                entry_price, peak, close, ma5, ma20, hold,
                cfg=STRATEGY, extra=extra, history=history,
            )

            pct = (close / entry_price - 1) * 100
            result.append({
                "股號": sid, "名稱": str(name),
                "進場日": str(entry_date),
                "成本": entry_price, "現價": close,
                "損益%": round(pct, 2),
                "持有(日)": hold,
                "MA5": round(ma5, 2) if ma5 else None,
                "MA20": round(ma20, 2) if ma20 else None,
                "K值": round(today_r["k_value"], 1) if today_r["k_value"] else None,
                "D值": round(today_r["d_value"], 1) if today_r["d_value"] else None,
                "出場訊號": reason if should_exit else "✅ 續抱",
                "_exit": should_exit,
            })
    return result


@st.cache_data(ttl=300)
def load_institutional(days: int = 5, top_n: int = 20, col: str = "total_net") -> pd.DataFrame:
    cutoff = (date.today() - timedelta(days=days + 3)).strftime("%Y-%m-%d")
    with get_session() as s:
        rows = s.execute(text(f"""
            SELECT i.stock_id, st.stock_name,
                   SUM(i.{col})::bigint      AS net,
                   SUM(ABS(i.{col}))::bigint AS turnover,
                   COUNT(*)                  AS days
            FROM institutional_trading i
            JOIN stocks st ON st.stock_id = i.stock_id
            WHERE i.trade_date >= :c
            GROUP BY i.stock_id, st.stock_name
            HAVING SUM(i.{col}) != 0
            ORDER BY net DESC
            LIMIT :n
        """), {"c": cutoff, "n": top_n}).fetchall()
    df = pd.DataFrame(rows, columns=["股號", "名稱", "累積買超(張)", "總成交(張)", "交易日"])
    return df


@st.cache_data(ttl=300)
def load_stock_ohlcv(sid: str, days: int = 120) -> pd.DataFrame:
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_session() as s:
        rows = s.execute(text("""
            SELECT p.trade_date, p.open, p.high, p.low, p.close, p.volume,
                   t.ma5, t.ma20, t.macd_hist, t.k_value, t.d_value
            FROM daily_prices p
            LEFT JOIN technical_indicators t
                ON t.stock_id = p.stock_id AND t.trade_date = p.trade_date
            WHERE p.stock_id = :sid AND p.trade_date >= :c AND p.close > 0
            ORDER BY p.trade_date ASC
        """), {"sid": sid, "c": cutoff}).fetchall()
    df = pd.DataFrame(rows, columns=["日期","開","高","低","收","量","MA5","MA20","MACD_Hist","K","D"])
    df["日期"] = pd.to_datetime(df["日期"])
    for c in ["開","高","低","收","量","MA5","MA20","MACD_Hist","K","D"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data(ttl=300)
def load_closed_positions() -> pd.DataFrame:
    with get_session() as s:
        rows = s.execute(text("""
            SELECT p.stock_id, st.stock_name,
                   p.entry_date, p.entry_price,
                   p.exit_date,  p.exit_price,
                   p.return_pct, p.exit_reason,
                   (p.exit_date - p.entry_date) AS hold_days
            FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
            WHERE p.status = 'closed'
            ORDER BY p.exit_date DESC
        """)).fetchall()
    df = pd.DataFrame(rows, columns=[
        "股號","名稱","進場日","進場價","出場日","出場價","報酬%","出場原因","持有天數"
    ])
    df["報酬%"] = pd.to_numeric(df["報酬%"], errors="coerce")
    return df


@st.cache_data(ttl=300)
def load_active_stock_ids() -> list[str]:
    with get_session() as s:
        rows = s.execute(text(
            "SELECT stock_id FROM stocks WHERE is_active=true ORDER BY stock_id"
        )).fetchall()
    return [r[0] for r in rows]


@st.cache_data(ttl=300)
def load_market_signals(days: int = 7, signal_type: str = None) -> pd.DataFrame:
    try:
        cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_session() as s:
            if signal_type:
                rows = s.execute(text("""
                    SELECT signal_type, source, title, summary, url,
                           related_stocks, sentiment, signal_date
                    FROM market_signals
                    WHERE signal_date >= :c AND signal_type = :t
                    ORDER BY signal_date DESC, id DESC
                    LIMIT 100
                """), {"c": cutoff, "t": signal_type}).fetchall()
            else:
                rows = s.execute(text("""
                    SELECT signal_type, source, title, summary, url,
                           related_stocks, sentiment, signal_date
                    FROM market_signals
                    WHERE signal_date >= :c
                    ORDER BY signal_date DESC, id DESC
                    LIMIT 200
                """), {"c": cutoff}).fetchall()
        df = pd.DataFrame(rows, columns=[
            "類型", "來源", "標題", "摘要", "網址", "相關股票", "情緒", "日期"
        ])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_etf_changes(days: int = 30) -> pd.DataFrame:
    try:
        cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_session() as s:
            rows = s.execute(text("""
                SELECT etf_name, stock_id, stock_name, change_type,
                       old_weight, new_weight, detected_date
                FROM etf_changes
                WHERE detected_date >= :c
                ORDER BY detected_date DESC, id DESC
                LIMIT 200
            """), {"c": cutoff}).fetchall()
        df = pd.DataFrame(rows, columns=[
            "ETF", "股號", "股名", "異動", "舊比重%", "新比重%", "偵測日"
        ])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_daily_digest(days: int = 5) -> tuple[date, str] | None:
    """取最近 days 天內最新一份每日彙整，回傳 (日期, 內文)；無則 None。"""
    try:
        with get_session() as s:
            row = s.execute(text("""
                SELECT signal_date, summary FROM market_signals
                WHERE signal_type = 'digest'
                  AND signal_date >= CURRENT_DATE - :days * INTERVAL '1 day'
                ORDER BY signal_date DESC, id DESC LIMIT 1
            """), {"days": days}).fetchone()
        return (row[0], row[1]) if row else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
#  Page 1：首頁 Dashboard
# ══════════════════════════════════════════════════════════════════
if page == "📊 首頁":
    st.title("📈 台股顧問系統")

    status = db_status()
    if not status["ok"]:
        st.error(f"資料庫連線失敗：{status['error']}")
        st.stop()

    positions = load_open_positions()
    exit_cnt  = sum(1 for p in positions if p["_exit"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("資料截止日",   str(status["last_date"]))
    c2.metric("追蹤股票數",   f"{status['stocks']:,} 檔")
    c3.metric("持倉中",       f"{status['open_pos']} 檔")
    c4.metric("出場訊號",     f"{exit_cnt} 檔",
              delta="需注意" if exit_cnt else None, delta_color="inverse")

    if exit_cnt:
        alerts = [f"**{p['股號']} {p['名稱']}**（{p['出場訊號']}）"
                  for p in positions if p["_exit"]]
        st.warning("⚠ 以下股票今日觸發出場訊號：\n\n" + "　".join(alerts))

    # 持倉摘要
    if positions:
        st.subheader("持倉概況")
        df = pd.DataFrame([{k: v for k, v in p.items() if k != "_exit"} for p in positions])
        avg_ret = df["損益%"].mean()
        best    = df.loc[df["損益%"].idxmax()]
        worst   = df.loc[df["損益%"].idxmin()]
        b1, b2, b3 = st.columns(3)
        b1.metric("平均損益",  f"{avg_ret:+.1f}%")
        b2.metric("最佳持倉",  f"{best['名稱']} {best['損益%']:+.1f}%")
        b3.metric("最差持倉",  f"{worst['名稱']} {worst['損益%']:+.1f}%")

        # 損益長條圖
        fig = px.bar(
            df.sort_values("損益%"),
            x="損益%", y="名稱",
            orientation="h",
            color="損益%",
            color_continuous_scale=["#e74c3c", "#ecf0f1", "#2ecc71"],
            color_continuous_midpoint=0,
            text="損益%",
            title="各持倉損益%",
        )
        fig.update_traces(texttemplate="%{text:+.1f}%", textposition="outside")
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=40, b=0),
                          coloraxis_showscale=False)
        st.plotly_chart(fig, use_container_width=True)

    # 法人近5日動向摘要
    st.subheader("法人近 5 日買超 Top 5")
    inst_df = load_institutional(days=5, top_n=5, col="total_net")
    if not inst_df.empty:
        st.dataframe(inst_df, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════
#  Page 2：持倉追蹤
# ══════════════════════════════════════════════════════════════════
elif page == "📦 持倉追蹤":
    st.title("📦 持倉追蹤")
    if st.button("🔄 重新整理"):
        load_open_positions.clear()

    positions = load_open_positions()
    if not positions:
        st.info("目前無持倉")
        st.stop()

    df = pd.DataFrame([{k: v for k, v in p.items() if k != "_exit"} for p in positions])
    exit_flags = [p["_exit"] for p in positions]

    # 色彩標記：出場訊號的列標紅
    def row_style(row):
        idx = df.index.get_loc(row.name)
        if exit_flags[idx]:
            return ["background-color: #fdecea"] * len(row)
        return [""] * len(row)

    st.dataframe(
        df.style.apply(row_style, axis=1)
          .format({"成本": "{:.2f}", "現價": "{:.2f}", "損益%": "{:+.2f}%",
                   "MA5": "{:.2f}", "MA20": "{:.2f}"}),
        use_container_width=True, hide_index=True
    )

    exit_pos = [p for p in positions if p["_exit"]]
    if exit_pos:
        st.error(f"⚠ {len(exit_pos)} 檔觸發出場訊號")
        for p in exit_pos:
            st.write(f"- **{p['股號']} {p['名稱']}**：{p['出場訊號']}（損益 {p['損益%']:+.2f}%）")


# ══════════════════════════════════════════════════════════════════
#  Page 3：法人動向
# ══════════════════════════════════════════════════════════════════
elif page == "🏦 法人動向":
    st.title("🏦 法人動向")

    col1, col2 = st.columns([1, 3])
    with col1:
        days   = st.selectbox("統計天數", [1, 3, 5, 10, 20], index=2)
        top_n  = st.slider("顯示檔數", 10, 50, 20)
        cat    = st.radio("類別", ["三大法人合計", "外資"])
        db_col = "foreign_net" if cat == "外資" else "total_net"

    with col2:
        st.subheader(f"{cat} 近 {days} 日累積買超 Top {top_n}")
        df_buy  = load_institutional(days=days, top_n=top_n, col=db_col)
        df_sell = load_institutional(days=days, top_n=top_n, col=db_col)
        # 賣超：ORDER BY ASC → 重新查
        cutoff = (date.today() - timedelta(days=days + 3)).strftime("%Y-%m-%d")
        with get_session() as s:
            rows_sell = s.execute(text(f"""
                SELECT i.stock_id, st.stock_name,
                       SUM(i.{db_col})::bigint AS net
                FROM institutional_trading i
                JOIN stocks st ON st.stock_id = i.stock_id
                WHERE i.trade_date >= :c
                GROUP BY i.stock_id, st.stock_name
                HAVING SUM(i.{db_col}) < 0
                ORDER BY net ASC LIMIT :n
            """), {"c": cutoff, "n": top_n}).fetchall()
        df_sell = pd.DataFrame(rows_sell, columns=["股號","名稱","累積賣超(張)"])

        t1, t2 = st.tabs(["🔺 買超排行", "🔻 賣超排行"])
        with t1:
            if df_buy.empty:
                st.info("無資料")
            else:
                fig = px.bar(df_buy.head(20), x="累積買超(張)", y="名稱",
                             orientation="h", color="累積買超(張)",
                             color_continuous_scale="Greens", title="買超前 20")
                fig.update_layout(height=500, coloraxis_showscale=False,
                                  margin=dict(l=0, r=0, t=40, b=0))
                st.plotly_chart(fig, use_container_width=True)
                st.dataframe(df_buy, use_container_width=True, hide_index=True)
        with t2:
            if df_sell.empty:
                st.info("無資料")
            else:
                fig = px.bar(df_sell.head(20), x="累積賣超(張)", y="名稱",
                             orientation="h", color="累積賣超(張)",
                             color_continuous_scale="Reds_r", title="賣超前 20")
                fig.update_layout(height=500, coloraxis_showscale=False,
                                  margin=dict(l=0, r=0, t=40, b=0))
                st.plotly_chart(fig, use_container_width=True)
                st.dataframe(df_sell, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════
#  Page 4：個股走勢
# ══════════════════════════════════════════════════════════════════
elif page == "📉 個股走勢":
    st.title("📉 個股走勢")

    stock_ids = load_active_stock_ids()
    col1, col2 = st.columns([1, 3])
    with col1:
        sid  = st.text_input("輸入股號", value="2884")
        days = st.selectbox("天數", [60, 120, 240], index=1)

    df = load_stock_ohlcv(sid, days=days)
    if df.empty:
        st.warning(f"找不到 {sid} 的資料")
        st.stop()

    # ── K線 + 均線 ──
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df["日期"], open=df["開"], high=df["高"], low=df["低"], close=df["收"],
        name="K線", increasing_line_color="#e74c3c", decreasing_line_color="#2ecc71",
    ))
    for ma, color in [("MA5","#f39c12"), ("MA20","#3498db")]:
        fig.add_trace(go.Scatter(x=df["日期"], y=df[ma], name=ma,
                                 line=dict(color=color, width=1.2)))
    fig.update_layout(title=f"{sid} 日 K 線", height=380,
                      xaxis_rangeslider_visible=False,
                      margin=dict(l=0, r=0, t=40, b=0))
    st.plotly_chart(fig, use_container_width=True)

    # ── 成交量 ──
    vol_color = ["#e74c3c" if c >= o else "#2ecc71"
                 for c, o in zip(df["收"], df["開"])]
    fig_vol = go.Figure(go.Bar(x=df["日期"], y=df["量"], marker_color=vol_color, name="成交量"))
    fig_vol.update_layout(title="成交量（張）", height=160,
                          margin=dict(l=0, r=0, t=30, b=0))
    st.plotly_chart(fig_vol, use_container_width=True)

    # ── KD ──
    fig_kd = go.Figure()
    fig_kd.add_trace(go.Scatter(x=df["日期"], y=df["K"], name="K",
                                line=dict(color="#e67e22", width=1.5)))
    fig_kd.add_trace(go.Scatter(x=df["日期"], y=df["D"], name="D",
                                line=dict(color="#9b59b6", width=1.5)))
    fig_kd.add_hline(y=80, line_dash="dash", line_color="red",   annotation_text="超買 80")
    fig_kd.add_hline(y=20, line_dash="dash", line_color="green", annotation_text="超賣 20")
    fig_kd.update_layout(title="KD 隨機指標", height=180,
                         margin=dict(l=0, r=0, t=30, b=0))
    st.plotly_chart(fig_kd, use_container_width=True)

    # ── MACD ──
    colors = ["#e74c3c" if v >= 0 else "#2ecc71" for v in df["MACD_Hist"].fillna(0)]
    fig_macd = go.Figure(go.Bar(x=df["日期"], y=df["MACD_Hist"],
                                marker_color=colors, name="MACD Hist"))
    fig_macd.update_layout(title="MACD 柱狀圖", height=160,
                           margin=dict(l=0, r=0, t=30, b=0))
    st.plotly_chart(fig_macd, use_container_width=True)


# ══════════════════════════════════════════════════════════════════
#  Page 5：歷史績效
# ══════════════════════════════════════════════════════════════════
elif page == "🔄 歷史績效":
    st.title("🔄 歷史績效")

    df = load_closed_positions()
    if df.empty:
        st.info("尚無已平倉記錄")
        st.stop()

    df["報酬%"] = pd.to_numeric(df["報酬%"], errors="coerce")
    wins   = df["報酬%"] > 0
    avg_r  = df["報酬%"].mean()
    win_r  = wins.mean() * 100
    avg_h  = pd.to_numeric(df["持有天數"], errors="coerce").mean()
    total  = (df["報酬%"] + 100).prod() ** (1 / len(df)) - 100  # 幾何平均

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("總交易筆數", len(df))
    c2.metric("勝率",       f"{win_r:.0f}%")
    c3.metric("平均報酬",   f"{avg_r:+.2f}%")
    c4.metric("平均持有",   f"{avg_h:.1f} 天")

    t1, t2 = st.tabs(["📊 報酬分布", "📋 交易紀錄"])
    with t1:
        fig = px.histogram(df, x="報酬%", nbins=30,
                           color_discrete_sequence=["#3498db"],
                           title="報酬率分布")
        fig.add_vline(x=0, line_dash="dash", line_color="red")
        fig.add_vline(x=avg_r, line_dash="dot", line_color="orange",
                      annotation_text=f"平均 {avg_r:+.1f}%")
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)

        # 出場原因統計
        reason_cnt = df["出場原因"].value_counts().reset_index()
        reason_cnt.columns = ["出場原因", "次數"]
        fig2 = px.pie(reason_cnt, names="出場原因", values="次數",
                      title="出場原因分布", hole=0.4)
        fig2.update_layout(height=320, margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig2, use_container_width=True)

    with t2:
        def color_ret(val):
            if isinstance(val, (int, float)):
                return "color: #e74c3c" if val < 0 else "color: #27ae60"
            return ""
        # pandas 2.1+ 將 Styler.applymap 改名為 Styler.map（3.0 移除 applymap）
        styler = df.style
        _elementwise = getattr(styler, "map", None) or styler.applymap
        st.dataframe(
            _elementwise(color_ret, subset=["報酬%"])
              .format({"進場價": "{:.2f}", "出場價": "{:.2f}", "報酬%": "{:+.2f}%"}),
            use_container_width=True, hide_index=True,
        )


# ══════════════════════════════════════════════════════════════════
#  Page 6：市場情報
# ══════════════════════════════════════════════════════════════════
elif page == "📰 市場情報":
    st.title("📰 市場情報")
    st.caption("ETF 換股公告、財經新聞 AI 摘要、YouTube 節目摘要（每日 21:00 自動更新）")

    days_filter = st.sidebar.selectbox("顯示天數", [3, 7, 14, 30], index=1)

    if st.button("🔄 重新整理"):
        load_market_signals.clear()
        load_etf_changes.clear()
        load_daily_digest.clear()

    # ── 每日彙整（置頂顯示，取最近一份）─────────────────────────
    digest_row = load_daily_digest()
    if digest_row:
        digest_date, digest_text = digest_row
        with st.expander(f"📋 {digest_date} 市場情報彙整（AI 自動生成）", expanded=True):
            st.markdown(digest_text)
    else:
        st.info("彙整尚未生成（每日 21:00 pipeline 跑完後自動產生）")

    st.markdown("---")
    tab1, tab2, tab3 = st.tabs(["🔀 ETF 換股", "📰 財經新聞", "▶️ YouTube"])

    # ── Tab 1：ETF 換股 ──────────────────────────────────────────
    with tab1:
        st.subheader("ETF 換股偵測")
        st.caption("追蹤主動型 ETF（如 00981A）及高股息 ETF 的持股異動")

        df_chg = load_etf_changes(days=days_filter)
        if df_chg.empty:
            st.info(f"近 {days_filter} 天無換股記錄（資料每日 21:00 更新，首次執行需先跑 Pipeline）")
        else:
            TYPE_EMOJI = {
                "added":     "🟢 新增",
                "removed":   "🔴 移除",
                "increased": "⬆️ 加碼",
                "decreased": "⬇️ 減碼",
            }
            df_chg["異動"] = df_chg["異動"].map(lambda x: TYPE_EMOJI.get(x, x))

            etf_list = ["全部"] + sorted(df_chg["ETF"].unique().tolist())
            selected_etf = st.selectbox("篩選 ETF", etf_list)
            if selected_etf != "全部":
                df_chg = df_chg[df_chg["ETF"] == selected_etf]

            def _etf_row_style(row):
                t = row["異動"]
                if "新增" in str(t) or "加碼" in str(t):
                    return ["background-color: #eafaf1"] * len(row)
                if "移除" in str(t) or "減碼" in str(t):
                    return ["background-color: #fdedec"] * len(row)
                return [""] * len(row)

            st.dataframe(
                df_chg.style.apply(_etf_row_style, axis=1)
                      .format({"舊比重%": "{:.2f}", "新比重%": "{:.2f}"}),
                use_container_width=True, hide_index=True,
            )

            added   = df_chg[df_chg["異動"].str.contains("新增|加碼", na=False)]
            removed = df_chg[df_chg["異動"].str.contains("移除|減碼", na=False)]
            c1, c2 = st.columns(2)
            c1.metric("買進訊號（新增/加碼）", len(added))
            c2.metric("賣出訊號（移除/減碼）", len(removed))

    # ── Tab 2：財經新聞 ──────────────────────────────────────────
    with tab2:
        st.subheader("財經新聞")

        sentiment_filter = st.radio(
            "情緒篩選", ["全部", "正面", "負面"], horizontal=True
        )
        df_news = load_market_signals(days=days_filter, signal_type=None)
        df_news = df_news[df_news["類型"].isin(["news", "mops"])] if not df_news.empty else df_news

        if not df_news.empty:
            if sentiment_filter == "正面":
                df_news = df_news[df_news["情緒"] == "positive"]
            elif sentiment_filter == "負面":
                df_news = df_news[df_news["情緒"] == "negative"]

        if df_news.empty:
            st.info(f"近 {days_filter} 天無新聞記錄（資料每日自動更新）")
        else:
            import ast as _ast
            SENT_BADGE = {"positive": "🟢 利多", "negative": "🔴 利空", "neutral": "⚪"}

            for _, row in df_news.iterrows():
                badge = SENT_BADGE.get(row["情緒"], "⚪")

                # 解析相關股票
                stocks_str = ""
                try:
                    raw_stocks = row["相關股票"]
                    if raw_stocks and str(raw_stocks) not in ("None", "[]", ""):
                        stocks = _ast.literal_eval(str(raw_stocks)) if isinstance(raw_stocks, str) else raw_stocks
                        if stocks:
                            stocks_str = "　`" + "、".join(str(s) for s in stocks[:6]) + "`"
                except Exception:
                    pass

                # 標題行
                title = row["標題"]
                url   = _clean_val(row["網址"])
                st.markdown(
                    f"{badge} **{title}**{stocks_str}",
                )

                # AI 摘要（核心內容）
                summary = _clean_val(row["摘要"])
                if summary and summary != title:
                    st.markdown(f"> {summary}")

                # 來源 + 連結（輔助資訊）
                meta = f"📅 {row['日期']}　{row['來源']}"
                if url:
                    meta += f"　[→ 閱讀原文]({url})"
                st.caption(meta)
                st.markdown("---")

    # ── Tab 3：YouTube ───────────────────────────────────────────
    with tab3:
        st.subheader("YouTube 財經節目")
        st.caption("AI 自動摘要每日最新影片，擷取提及的股票代號與市場觀點")

        df_yt = load_market_signals(days=days_filter, signal_type="youtube")
        if df_yt.empty:
            st.info(f"近 {days_filter} 天無 YouTube 記錄")
        else:
            SENT_BADGE = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}
            for _, row in df_yt.iterrows():
                badge = SENT_BADGE.get(row["情緒"], "⚪")
                stocks_str = ""
                if row["相關股票"] and str(row["相關股票"]) not in ("None", "[]"):
                    try:
                        import ast
                        stocks = ast.literal_eval(str(row["相關股票"])) if isinstance(row["相關股票"], str) else row["相關股票"]
                        if stocks:
                            stocks_str = f"  提及：`{'、'.join(stocks[:8])}`"
                    except Exception:
                        pass

                summary_txt = _clean_val(row["摘要"])
                url_txt     = _clean_val(row["網址"])
                with st.expander(f"{badge} {row['標題']} — {row['日期']}"):
                    if summary_txt:
                        st.markdown(summary_txt)
                    else:
                        st.caption("（此影片無字幕，AI 無法生成摘要）")
                    if stocks_str:
                        st.markdown(stocks_str)
                    if url_txt:
                        st.markdown(f"[▶️ 觀看影片]({url_txt})")


# ══════════════════════════════════════════════════════════════════
#  Page 7：聰明資金追蹤
# ══════════════════════════════════════════════════════════════════
elif page == "🧠 聰明資金":
    st.title("🧠 聰明資金追蹤")
    st.caption("統一台股增長 (00981A) 換股動態 × 投信連續買超 — 尋找波段機會")

    TRACK_ETF   = "00981A"
    inv_days    = st.sidebar.slider("投信回看天數", 10, 30, 15)
    etf_days    = st.sidebar.slider("ETF換股回看天數", 14, 90, 45)
    min_buy_d   = st.sidebar.slider("投信最少買超天數", 2, 7, 3)
    min_single  = st.sidebar.number_input("單日大量門檻（張）", 100, 5000, 500, step=100)

    if st.button("🔄 重新整理"):
        st.cache_data.clear()

    # ── 即時查詢（不快取，確保最新）────────────────────────────
    def _query_invest(days, min_days, min_single_v):
        try:
            with get_session() as s:
                rows = s.execute(text("""
                    WITH w AS (
                        SELECT it.stock_id, it.trade_date, it.invest_net,
                               s.stock_name,
                               ind.name_zh AS industry
                        FROM institutional_trading it
                        JOIN stocks s ON it.stock_id = s.stock_id
                        LEFT JOIN industries ind ON s.industry_code = ind.code
                        WHERE it.trade_date >= CURRENT_DATE - :days * INTERVAL '1 day'
                    )
                    SELECT
                        stock_id,
                        MAX(stock_name)                                     AS 股票名稱,
                        MAX(industry)                                       AS 產業,
                        COUNT(*) FILTER (WHERE invest_net > 0)              AS 買超天數,
                        COUNT(*) FILTER (WHERE invest_net < 0)              AS 賣超天數,
                        -- invest_net 單位為股，÷1000 轉成張
                        ROUND(COALESCE(SUM(invest_net) FILTER (WHERE invest_net > 0), 0) / 1000.0) AS 累計買超張,
                        ROUND(MAX(invest_net) / 1000.0)                     AS 單日峰值張,
                        MAX(trade_date) FILTER (WHERE invest_net > 0)       AS 最後買超日
                    FROM w
                    GROUP BY stock_id
                    HAVING
                        COUNT(*) FILTER (WHERE invest_net > 0) >= :min_days
                        OR MAX(invest_net) >= :min_s * 1000
                    ORDER BY 買超天數 DESC, 累計買超張 DESC
                    LIMIT 50
                """), {"days": days, "min_days": min_days, "min_s": min_single_v}).fetchall()
            return pd.DataFrame(rows)
        except Exception as e:
            st.error(f"查詢失敗: {e}")
            return pd.DataFrame()

    def _query_etf(etf_id, days):
        try:
            with get_session() as s:
                rows = s.execute(text("""
                    SELECT
                        ec.stock_id                                         AS 股票代號,
                        ec.stock_name                                       AS 股票名稱,
                        CASE ec.change_type
                            WHEN 'added'     THEN '🆕 新納入'
                            WHEN 'increased' THEN '⬆ 加碼'
                            ELSE ec.change_type
                        END                                                 AS 動作,
                        COALESCE(ec.old_weight, 0)                          AS 舊權重,
                        COALESCE(ec.new_weight, 0)                          AS 新權重,
                        ROUND(COALESCE(ec.new_weight,0)
                              - COALESCE(ec.old_weight,0), 4)               AS 權重變化,
                        ec.detected_date                                    AS 偵測日期
                    FROM etf_changes ec
                    WHERE ec.etf_id = :etf_id
                      AND ec.change_type IN ('added','increased')
                      AND ec.detected_date >= CURRENT_DATE - :days * INTERVAL '1 day'
                    ORDER BY ec.detected_date DESC, 權重變化 DESC
                """), {"etf_id": etf_id, "days": days}).fetchall()
            return pd.DataFrame(rows)
        except Exception as e:
            st.error(f"查詢失敗: {e}")
            return pd.DataFrame()

    def _query_etf_holdings(etf_id):
        try:
            with get_session() as s:
                rows = s.execute(text("""
                    SELECT stock_id AS 代號, stock_name AS 名稱,
                           ROUND(weight_pct, 2) AS 權重百分比,
                           snapshot_date AS 快照日期
                    FROM etf_holdings
                    WHERE etf_id = :eid
                      AND snapshot_date = (
                          SELECT MAX(snapshot_date) FROM etf_holdings WHERE etf_id = :eid
                      )
                    ORDER BY weight_pct DESC NULLS LAST
                    LIMIT 20
                """), {"eid": etf_id}).fetchall()
            return pd.DataFrame(rows)
        except Exception:
            return pd.DataFrame()

    df_invest = _query_invest(inv_days, min_buy_d, min_single)
    df_etf    = _query_etf(TRACK_ETF, etf_days)

    # 計算重疊
    overlap_ids = set()
    if not df_invest.empty and not df_etf.empty:
        invest_ids = set(df_invest["stock_id"].tolist()) if "stock_id" in df_invest.columns else set()
        etf_ids    = set(df_etf["股票代號"].tolist())
        overlap_ids = invest_ids & etf_ids

    # ── Tab 布局 ────────────────────────────────────────────────
    tab_gold, tab_etf, tab_invest = st.tabs([
        f"⭐ 黃金交叉（{len(overlap_ids)}）",
        f"📊 統一ETF動態（{len(df_etf)}筆）",
        f"🏦 投信連買排行（{len(df_invest)}支）",
    ])

    # ── Tab 1：黃金交叉 ─────────────────────────────────────────
    with tab_gold:
        st.subheader("⭐ 雙重確認訊號")
        st.caption("同時出現在「投信連買」與「統一ETF加碼/新增」的股票——波段勝率最高")

        if not overlap_ids:
            if df_invest.empty or df_etf.empty:
                st.info("資料尚未齊全（ETF換股需至少執行兩次 pipeline 後才有記錄）")
            else:
                st.info(f"目前回看期間內（投信{inv_days}日 / ETF{etf_days}日）無重疊訊號")
        else:
            for sid in sorted(overlap_ids):
                inv_row = df_invest[df_invest["stock_id"] == sid].iloc[0] if not df_invest.empty else None
                etf_row = df_etf[df_etf["股票代號"] == sid].iloc[0] if not df_etf.empty else None
                name = (inv_row["股票名稱"] if inv_row is not None else
                        etf_row["股票名稱"] if etf_row is not None else "")

                with st.container(border=True):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown(f"### {sid} {name}")
                        if inv_row is not None:
                            st.markdown(
                                f"**🏦 投信：** 近{inv_days}日買超 **{inv_row['買超天數']}** 天 "
                                f"｜ 累計 **{inv_row['累計買超張']:,.0f}** 張 "
                                f"｜ 峰值 **{inv_row['單日峰值張']:,.0f}** 張/日"
                            )
                    with col2:
                        if etf_row is not None:
                            st.markdown(
                                f"**📊 統一ETF：** {etf_row['動作']} "
                                f"（{etf_row['偵測日期']}）  \n"
                                f"權重 {etf_row['舊權重']:.2f}% → **{etf_row['新權重']:.2f}%** "
                                f"（+{etf_row['權重變化']:.2f}%）"
                            )

    # ── Tab 2：統一ETF動態 ──────────────────────────────────────
    with tab_etf:
        st.subheader(f"📊 {TRACK_ETF} 統一台股增長 — 近{etf_days}日加碼/新增")
        st.caption("主動型 ETF 操盤手看好的股票，任意日均可換股，值得密切追蹤")

        if df_etf.empty:
            st.info(f"近{etf_days}天無換股記錄（ETF 換股偵測需至少兩次 pipeline 後才有資料）")
        else:
            for _, r in df_etf.iterrows():
                weight_delta = r["權重變化"]
                delta_str = f"+{weight_delta:.2f}%" if weight_delta > 0 else f"{weight_delta:.2f}%"
                st.markdown(
                    f"{r['動作']} **{r['股票代號']} {r['股票名稱']}**"
                    f"　權重 {r['舊權重']:.2f}% → **{r['新權重']:.2f}%**（{delta_str}）"
                    f"　📅 {r['偵測日期']}"
                    + ("　⭐" if r["股票代號"] in overlap_ids else "")
                )

        st.markdown("---")
        st.subheader(f"最新持股明細（前 20）")
        df_hold = _query_etf_holdings(TRACK_ETF)
        if df_hold.empty:
            st.info("尚無持股快照（pipeline 執行後自動更新）")
        else:
            snapshot = df_hold["快照日期"].iloc[0] if not df_hold.empty else "—"
            st.caption(f"資料日期：{snapshot}")
            st.dataframe(
                df_hold.drop(columns=["快照日期"]),
                use_container_width=True,
                hide_index=True,
            )

    # ── Tab 3：投信連買排行 ─────────────────────────────────────
    with tab_invest:
        st.subheader(f"🏦 投信連買排行 — 近{inv_days}日買超≥{min_buy_d}天 或 單日≥{int(min_single)}張")
        st.caption("投信（信託投資公司）持續淨買超代表主力資金建倉，配合技術面做波段")

        if df_invest.empty:
            st.info("近期無符合條件的投信買超記錄（資料每日自動更新）")
        else:
            display_cols = ["stock_id", "股票名稱", "產業", "買超天數",
                            "賣超天數", "累計買超張", "單日峰值張", "最後買超日"]
            df_show = df_invest[[c for c in display_cols if c in df_invest.columns]].copy()
            # ROUND 回傳 Decimal，轉成 int 以利顯示與格式化
            for c in ("累計買超張", "單日峰值張", "買超天數", "賣超天數"):
                if c in df_show.columns:
                    df_show[c] = df_show[c].fillna(0).astype("int64")
            df_show.insert(0, "標記", df_show["stock_id"].apply(
                lambda x: "⭐" if x in overlap_ids else ""
            ))
            df_show = df_show.rename(columns={"stock_id": "代號"})
            st.dataframe(
                df_show,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "標記":       st.column_config.TextColumn("", width="small"),
                    "買超天數":   st.column_config.NumberColumn("買超天數", format="%d 天"),
                    "累計買超張": st.column_config.NumberColumn("累計買超(張)", format="%,d"),
                    "單日峰值張": st.column_config.NumberColumn("峰值(張)", format="%,d"),
                },
            )
