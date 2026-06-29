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
    ["📊 首頁", "📦 持倉追蹤", "🏦 法人動向", "📉 個股走勢", "🔄 歷史績效"],
)
st.sidebar.markdown("---")

# 立即更新按鈕
if st.sidebar.button("🔄 立即更新資料", help="更新全市場價格 + 熱門200支指標，目標 <1 分鐘"):
    import subprocess
    with st.sidebar.status("更新中（約 30-60 秒）...", expanded=True) as status:
        r = subprocess.run(
            [sys.executable, "run_pipeline.py", "--mode", "quick"],
            capture_output=True, text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=120,
        )
        if r.returncode == 0:
            st.write("✅ 價格 + 技術指標更新完成")
        else:
            st.write(f"❌ 更新失敗：{r.stderr[-300:]}")
        status.update(label="✅ 完成" if r.returncode == 0 else "❌ 失敗", state="complete")
    st.cache_data.clear()
    st.rerun()

st.sidebar.caption(f"今日：{date.today()}")


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
        st.dataframe(
            df.style.applymap(color_ret, subset=["報酬%"])
              .format({"進場價": "{:.2f}", "出場價": "{:.2f}", "報酬%": "{:+.2f}%"}),
            use_container_width=True, hide_index=True,
        )
