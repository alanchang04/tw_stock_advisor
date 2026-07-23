"""
app.py — 台股顧問系統 Streamlit 網頁介面

啟動方式：
    streamlit run app.py
或雙擊 run_app.bat
"""
import sys, os
import json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
from sqlalchemy import text

from database.connection import get_session
from agent.strategy import decide_exit, suggest_shares, STRATEGY

# ══════════════════════════════════════════════════════════════════
#  頁面設定
# ══════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="台股顧問系統",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ══════════════════════════════════════════════════════════════════
#  登入（多使用者：每人只看得到自己的持倉與清單；市場資料共享）
# ══════════════════════════════════════════════════════════════════
from database.users import authenticate

if "auth_user" not in st.session_state:
    st.session_state.auth_user = None

if st.session_state.auth_user is None:
    st.title("🔐 台股顧問系統")
    st.caption("請登入（帳號由管理員建立）")
    with st.form("login_form"):
        _u = st.text_input("帳號")
        _p = st.text_input("密碼", type="password")
        _go = st.form_submit_button("登入")
    if _go:
        _info = authenticate(_u, _p)
        if _info:
            st.session_state.auth_user = _info
            st.rerun()
        else:
            st.error("帳號或密碼錯誤")
    st.stop()

USER = st.session_state.auth_user   # {user_id, username, display_name, role}

st.sidebar.title("📈 台股顧問")
_pages = ["📊 首頁", "📦 持倉追蹤", "🔖 追蹤清單", "🔎 個股分析", "🎯 練習軌", "🔥 族群輪動", "🏦 法人動向",
          "📉 個股走勢", "🔄 歷史績效", "📰 市場情報", "🧠 聰明資金", "🔍 決策軌跡"]
if USER["role"] == "admin":
    _pages.append("👤 帳號管理")
page = st.sidebar.radio("導覽", _pages)

_c1, _c2 = st.sidebar.columns([3, 1])
_c1.caption(f"👤 {USER['display_name']}（{USER['role']}）")
if _c2.button("登出"):
    st.session_state.auth_user = None
    st.rerun()
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
            # 2026-07-22：這裡原本不分來源混算open_pos，首頁「持倉中」metric會把AI持倉跟
            # 使用者自己手動記的持倉混在一起，跟頁面下方只顯示AI持倉的圖表對不上——分開算。
            ai_pos_c = s.execute(text(
                "SELECT COUNT(*) FROM positions WHERE status='open' AND COALESCE(source,'ai')='ai'"
            )).scalar()
            manual_pos_c = s.execute(text(
                "SELECT COUNT(*) FROM positions WHERE status='open' AND source='manual'"
            )).scalar()
            sig_t = s.execute(text(
                "SELECT COUNT(*) FROM market_signals WHERE signal_date = CURRENT_DATE"
            )).scalar()
        return {"ok": True, "last_date": last, "stocks": sc,
                "open_pos": ai_pos_c, "manual_pos": manual_pos_c, "signals_today": sig_t}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# 側欄資料時間指示：按「立即更新」後看這裡就知道新資料落地了沒
# （pipeline 約 5-10 分鐘；此快取 5 分鐘，按「🔄」可強制刷新）
_dbs = db_status()
if _dbs.get("ok"):
    _c_info, _c_btn = st.sidebar.columns([4, 1])
    _c_info.caption(f"📅 行情資料至 {_dbs['last_date']}｜今日情報 {_dbs.get('signals_today', 0)} 筆")
    if _c_btn.button("🔄", help="重新讀取資料狀態（清除快取）"):
        st.cache_data.clear()
        st.rerun()


@st.cache_data(ttl=60)
def load_open_positions() -> list[dict]:
    today = date.today()
    result = []
    with get_session() as s:
        rows = s.execute(text("""
            SELECT p.stock_id, st.stock_name, p.entry_date, p.entry_price, p.peak_price
            FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
            WHERE p.status = 'open' AND COALESCE(p.source, 'ai') = 'ai'
            ORDER BY p.entry_date
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
            # 2026-07-22：AI持倉的實際張數目前沒有存進DB（positions.shares只有手動倉在用），
            # 這裡用跟下單當時同一套1%風險法則(suggest_shares)反推「照現在資金設定，
            # 這張單大概會買多少股」，換算成金額損益——不是精確的歷史成交量，是可視化用估計值。
            est_shares = suggest_shares(entry_price, cfg=STRATEGY)
            pnl_dollar = (close - entry_price) * est_shares
            result.append({
                "股號": sid, "名稱": str(name),
                "進場日": str(entry_date),
                "成本": entry_price, "現價": close,
                "損益%": round(pct, 2),
                "損益$（估）": round(pnl_dollar),
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
    # 2026-07-22 修1000×單位bug：institutional_trading.*_net 單位是「股」，這裡原本
    # 直接 SUM 卻把欄位標成「(張)」，法人動向頁與首頁Top5的數字都被灌大1000倍
    # （南亞近5日171,783,000股被顯示成「171,783,000張」，正確是171,783張）。÷1000轉張。
    cutoff = (date.today() - timedelta(days=days + 3)).strftime("%Y-%m-%d")
    with get_session() as s:
        rows = s.execute(text(f"""
            SELECT i.stock_id, st.stock_name,
                   ROUND(SUM(i.{col}) / 1000.0)::bigint      AS net,
                   ROUND(SUM(ABS(i.{col})) / 1000.0)::bigint AS turnover,
                   COUNT(*)                                  AS days
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
    # 2026-07-22：原本沒過濾來源，🔄歷史績效頁號稱是「AI每筆已平倉交易」的績效，
    # 但這裡其實會把使用者自己手動記的已平倉持倉也混進去，污染AI實際表現的統計——
    # 跟首頁持倉數混AI/手動是同一類問題，這裡也補上過濾。
    with get_session() as s:
        rows = s.execute(text("""
            SELECT p.stock_id, st.stock_name,
                   p.entry_date, p.entry_price,
                   p.exit_date,  p.exit_price,
                   p.return_pct, p.exit_reason,
                   (p.exit_date - p.entry_date) AS hold_days
            FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
            WHERE p.status = 'closed' AND COALESCE(p.source, 'ai') = 'ai'
            ORDER BY p.exit_date DESC
        """)).fetchall()
    df = pd.DataFrame(rows, columns=[
        "股號","名稱","進場日","進場價","出場日","出場價","報酬%","出場原因","持有天數"
    ])
    df["報酬%"] = pd.to_numeric(df["報酬%"], errors="coerce")
    if not df.empty:
        # 損益$（估）：跟開倉部位同一套邏輯，用目前資金設定反推張數，不是實際歷史成交量
        from agent.strategy import suggest_shares
        df["損益$（估）"] = [
            round((float(r["出場價"]) - float(r["進場價"])) * suggest_shares(float(r["進場價"]), cfg=STRATEGY))
            for _, r in df.iterrows()
        ]
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
#  決策軌跡卡片元件（共用：決策軌跡頁 + 個股分析頁）
#  狀態色一律搭配圖示與文字標籤，不單靠顏色傳達
# ══════════════════════════════════════════════════════════════════
import html as _htmlmod

_DT_CSS = """
<style>
.dt-card{background:rgba(128,138,160,.08);border:1px solid rgba(128,138,160,.22);
  border-radius:10px;padding:.65rem .8rem;margin-bottom:.55rem;font-size:.85rem;line-height:1.5}
.dt-ok{border-left:4px solid #34a06b}
.dt-warn{border-left:4px solid #d9a441}
.dt-bad{border-left:4px solid #d05252}
.dt-info{border-left:4px solid #5b84d6}
.dt-head{font-weight:700;margin-bottom:.3rem;display:flex;justify-content:space-between;gap:.5rem;align-items:baseline}
.dt-chip{display:inline-block;padding:.02rem .5rem;border-radius:99px;font-size:.72rem;
  background:rgba(91,132,214,.16);border:1px solid rgba(91,132,214,.4);margin:0 .18rem .18rem 0}
.dt-chip.g{background:rgba(52,160,107,.14);border-color:rgba(52,160,107,.45)}
.dt-chip.o{background:rgba(217,164,65,.14);border-color:rgba(217,164,65,.5)}
.dt-num{font-size:.72rem;opacity:.65;white-space:nowrap}
.dt-scroll{max-height:460px;overflow-y:auto;white-space:pre-wrap}
.dt-col-title{font-weight:800;font-size:.95rem;margin:.15rem 0 .5rem 0}
.dt-badge{background:rgba(91,132,214,.3);border-radius:6px;padding:0 .4rem;font-size:.72rem;font-weight:700}
.dt-verdict{background:rgba(52,160,107,.10);border:1px solid rgba(52,160,107,.45);
  border-radius:12px;padding:.8rem .9rem;margin-bottom:.6rem;font-size:.9rem;line-height:1.55}
</style>"""


def _dt_esc(t):
    return _htmlmod.escape(str(t)).replace("\n", "<br>")


def _dt_i(v):
    return 0 if v is None or pd.isna(v) else int(v)


def _dt_card(cls, icon, title, body_html, meta=""):
    return (f"<div class='dt-card {cls}'><div class='dt-head'>"
            f"<span>{icon} {title}</span>{meta}</div>{body_html}</div>")


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

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("資料截止日",   str(status["last_date"]))
    c2.metric("追蹤股票數",   f"{status['stocks']:,} 檔")
    c3.metric("AI持倉中",     f"{status['open_pos']} 檔")
    c4.metric("手動持倉",     f"{status.get('manual_pos', 0)} 檔",
              help="自己記的持倉（📦持倉追蹤頁「我的持倉」分頁），不算進下面的AI持倉統計")
    c5.metric("出場訊號",     f"{exit_cnt} 檔",
              delta="需注意" if exit_cnt else None, delta_color="inverse")

    if exit_cnt:
        alerts = [f"**{p['股號']} {p['名稱']}**（{p['出場訊號']}）"
                  for p in positions if p["_exit"]]
        st.warning("⚠ 以下股票今日觸發出場訊號：\n\n" + "　".join(alerts))

    # 持倉摘要
    if positions:
        st.subheader("持倉概況")
        st.caption("損益$為估計值：用目前資金設定（1%風險法則）反推張數，"
                   "不是實際歷史成交量——目的是看出「%數差不多，賺的錢差很多」的規模差異")
        df = pd.DataFrame([{k: v for k, v in p.items() if k != "_exit"} for p in positions])
        avg_ret     = df["損益%"].mean()
        total_pnl   = df["損益$（估）"].sum()
        best        = df.loc[df["損益%"].idxmax()]
        worst       = df.loc[df["損益%"].idxmin()]
        b1, b2, b3, b4 = st.columns(4)
        b1.metric("平均損益",    f"{avg_ret:+.1f}%")
        b2.metric("總損益$（估）", f"{total_pnl:+,.0f}")
        b3.metric("最佳持倉",    f"{best['名稱']} {best['損益%']:+.1f}%")
        b4.metric("最差持倉",    f"{worst['名稱']} {worst['損益%']:+.1f}%")

        # 損益長條圖（同時標%數跟估計金額，避免「%數差不多但賺的錢差很多」被忽略）
        fig = px.bar(
            df.sort_values("損益%"),
            x="損益%", y="名稱",
            orientation="h",
            color="損益%",
            color_continuous_scale=["#e74c3c", "#ecf0f1", "#2ecc71"],
            color_continuous_midpoint=0,
            text="損益%",
            custom_data=["損益$（估）"],
            title="各持倉損益%（懸停看估計金額）",
        )
        fig.update_traces(texttemplate="%{text:+.1f}%", textposition="outside",
                          hovertemplate="%{y}：%{x:+.1f}%｜約 %{customdata[0]:+,.0f} 元<extra></extra>")
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

    tab_ai, tab_manual = st.tabs(["🤖 AI 部位", "👤 我的持倉"])

    # ── Tab 1：AI 部位（系統推薦、自動出場）─────────────────────
    with tab_ai:
        positions = load_open_positions()
        if not positions:
            st.info("目前無 AI 持倉")
        else:
            df = pd.DataFrame([{k: v for k, v in p.items() if k != "_exit"} for p in positions])
            exit_flags = [p["_exit"] for p in positions]

            # 色彩標記：出場訊號的列標紅
            def row_style(row):
                idx = df.index.get_loc(row.name)
                if exit_flags[idx]:
                    return ["background-color: rgba(231, 76, 60, 0.25)"] * len(row)
                return [""] * len(row)

            st.caption("損益$（估）：用目前資金設定反推張數估算，非實際歷史成交量")
            st.dataframe(
                df.style.apply(row_style, axis=1)
                  .format({"成本": "{:.2f}", "現價": "{:.2f}", "損益%": "{:+.2f}%",
                           "損益$（估）": "{:+,.0f}",
                           "MA5": "{:.2f}", "MA20": "{:.2f}"}, na_rep="—"),
                use_container_width=True, hide_index=True
            )

            exit_pos = [p for p in positions if p["_exit"]]
            if exit_pos:
                st.error(f"⚠ {len(exit_pos)} 檔觸發出場訊號")
                for p in exit_pos:
                    st.write(f"- **{p['股號']} {p['名稱']}**：{p['出場訊號']}（損益 {p['損益%']:+.2f}%）")

    # ── Tab 2：我的持倉（手動建倉、AI 只建議不自動平倉）─────────
    with tab_manual:
        st.caption("輸入實際持有的股票，每日 pipeline 自動判斷：⚠️建議賣出 / ➕可加碼 / ✅續抱")

        # 新增持倉表單（帳本：幫自己/他人分開記，如「我的」「媽媽的」）
        with st.form("add_manual_position", clear_on_submit=True):
            c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 1])
            f_sid    = c1.text_input("股票代號", placeholder="2330")
            f_date   = c2.date_input("買入日期", value=date.today())
            f_price  = c3.number_input("買入價格", min_value=0.01, step=0.01, format="%.2f")
            f_shares = c4.number_input("股數（1張=1000股）", min_value=1, step=1000, value=1000)
            f_label  = c5.text_input("帳本", value="我的", help="幫別人記就填名字，如「媽媽的」")
            f_note   = st.text_input("備註（選填）", placeholder="例：AI 伺服器題材")
            submitted = st.form_submit_button("➕ 新增持倉")

        if submitted:
            sid = f_sid.strip()
            with get_session() as s:
                exists = s.execute(text(
                    "SELECT stock_name FROM stocks WHERE stock_id = :sid"
                ), {"sid": sid}).fetchone()
            if not exists:
                st.error(f"代號 {sid} 不存在於股票清單，請確認")
            else:
                with get_session() as s:
                    s.execute(text("""
                        INSERT INTO positions
                            (stock_id, entry_date, entry_price, shares,
                             entry_reason, source, status, user_id, account_label)
                        VALUES (:sid, :d, :p, :sh, :note, 'manual', 'open', :uid, :lb)
                    """), {"sid": sid, "d": f_date, "p": f_price,
                           "sh": int(f_shares), "note": f_note or "手動建倉",
                           "uid": USER["user_id"], "lb": (f_label or "我的").strip()})
                st.success(f"已新增 {sid} {exists[0]}：{f_date} @ {f_price:.2f} × {int(f_shares):,} 股"
                           f"（帳本：{(f_label or '我的').strip()}）")

        # 持倉列表（只看自己的；含現價與 AI 建議）
        with get_session() as s:
            mrows = s.execute(text("""
                SELECT p.id, p.stock_id, st.stock_name, p.entry_date,
                       p.entry_price, p.shares, p.last_advice, p.advice_date,
                       COALESCE(p.account_label, '我的') AS label,
                       (SELECT d.close FROM daily_prices d
                        WHERE d.stock_id = p.stock_id AND d.close > 0
                        ORDER BY d.trade_date DESC LIMIT 1) AS cur_close
                FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
                WHERE p.source = 'manual' AND p.status = 'open'
                  AND p.user_id = :uid
                ORDER BY label, p.entry_date
            """), {"uid": USER["user_id"]}).fetchall()

        if not mrows:
            st.info("尚無手動持倉，用上方表單新增")
        else:
            recs = []
            for pid, sid, name, ed, ep, sh, adv, adv_d, label, cur in mrows:
                ep = float(ep); cur = float(cur) if cur else None
                sh = int(sh) if sh else 0
                pnl_pct = (cur / ep - 1) * 100 if cur else None
                pnl_amt = (cur - ep) * sh if cur else None
                recs.append({
                    "編號": pid, "帳本": label, "代號": sid, "名稱": name, "買入日": ed,
                    "買入價": ep, "股數": sh,
                    "現價": cur, "損益%": pnl_pct, "損益金額": pnl_amt,
                    "AI 建議": _clean_val(adv) or "（今晚 pipeline 後產生）",
                    "建議日": _clean_val(adv_d) or "—",
                })

            # 帳本過濾
            labels = sorted({r["帳本"] for r in recs})
            if len(labels) > 1:
                pick_label = st.selectbox("帳本篩選", ["全部"] + labels)
                if pick_label != "全部":
                    recs = [r for r in recs if r["帳本"] == pick_label]
            dfm = pd.DataFrame(recs)

            def advice_style(v):
                v = str(v)
                if "賣出" in v:
                    return "color: #e74c3c; font-weight: bold"
                if "加碼" in v:
                    return "color: #27ae60; font-weight: bold"
                return ""

            st.dataframe(
                dfm.style.map(advice_style, subset=["AI 建議"])
                   .format({"買入價": "{:.2f}", "現價": "{:.2f}",
                            "損益%": "{:+.2f}%", "股數": "{:,d}",
                            "損益金額": "{:+,.0f}"}, na_rep="—"),
                use_container_width=True, hide_index=True,
            )

            total_pnl = sum(r["損益金額"] for r in recs if r["損益金額"] is not None)
            st.metric("合計未實現損益", f"{total_pnl:+,.0f} 元")

            # 平倉表單
            st.markdown("---")
            with st.form("close_manual_position"):
                c1, c2, c3 = st.columns([2, 1, 1])
                options = {f"#{r['編號']} {r['代號']} {r['名稱']}（{r['買入日']} @ {r['買入價']:.2f}）": r
                           for r in recs}
                pick = c1.selectbox("選擇要平倉的持倉", list(options.keys()))
                sell_price = c2.number_input("賣出價格", min_value=0.01, step=0.01, format="%.2f")
                closed = c3.form_submit_button("✅ 已賣出")
            if closed and pick:
                r = options[pick]
                ret_pct = (sell_price / r["買入價"] - 1) * 100
                with get_session() as s:
                    s.execute(text("""
                        UPDATE positions
                        SET status='closed', exit_date=:d, exit_price=:p,
                            exit_reason='手動平倉', return_pct=:r
                        WHERE id = :pid
                    """), {"d": date.today(), "p": sell_price,
                           "r": round(ret_pct, 4), "pid": r["編號"]})
                st.success(f"已平倉 {r['代號']} {r['名稱']}：報酬 {ret_pct:+.2f}%")
                st.rerun()


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
                       ROUND(SUM(i.{db_col}) / 1000.0)::bigint AS net
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

    # 策略版本過濾：2026-07-20 是最近一次策略大改的定案日（P1因子IC重新配權+出場規則
    # 消融+熊市擋新倉設為預設+族群曝險上限，見agent/strategy.py），也是「有熊市防護」
    # 的真正分界線。2026-07-22修正：這裡原本切在07-05，那個日期之後、07-20之前進場的
    # 交易用的其實是還沒防熊市的舊版策略（會一直買然後停損殺出），混進「新策略」績效
    # 裡會讓現在這版看起來比實際更差——之前用07-05這個舊分界線是2026-07-04出場規則
    # 改版時定的，後來策略又大改了好幾次，沒有跟著更新。
    STRATEGY_V2_DATE = date(2026, 7, 20)
    era = st.radio("評估範圍",
                   [f"🆕 現行策略（{STRATEGY_V2_DATE} 後進場，含熊市擋新倉防護）",
                    "📜 全部歷史（含舊策略，舊策略無熊市防護，僅供對照）"],
                   horizontal=True)

    df = load_closed_positions()
    if not df.empty and era.startswith("🆕"):
        df = df[pd.to_datetime(df["進場日"]).dt.date >= STRATEGY_V2_DATE]

    if df.empty:
        if era.startswith("🆕"):
            st.info(f"新策略（{STRATEGY_V2_DATE} 後進場）尚無已平倉交易——這是正常的，"
                    "波段平均持有約 3~4 週，觀察期需要一到兩個月累積樣本。"
                    "可切「全部歷史」看舊策略對照組。")
        else:
            st.info("尚無已平倉記錄")
        st.stop()

    df["報酬%"] = pd.to_numeric(df["報酬%"], errors="coerce")
    wins   = df["報酬%"] > 0
    avg_r  = df["報酬%"].mean()
    win_r  = wins.mean() * 100
    avg_h  = pd.to_numeric(df["持有天數"], errors="coerce").mean()
    total  = (df["報酬%"] + 100).prod() ** (1 / len(df)) - 100  # 幾何平均

    # 進階指標：逐筆權益曲線（依出場日）→ MDD / 獲利因子
    df_seq = df.sort_values("出場日").copy()
    df_seq["累計%"] = df_seq["報酬%"].cumsum()
    mdd = (df_seq["累計%"] - df_seq["累計%"].cummax()).min()
    g_win  = df.loc[wins, "報酬%"].sum()
    g_loss = abs(df.loc[~wins, "報酬%"].sum())
    pf = g_win / g_loss if g_loss > 0 else float("inf")

    total_pnl_dollar = df["損益$（估）"].sum() if "損益$（估）" in df.columns else None

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("總交易筆數", len(df))
    c2.metric("勝率",       f"{win_r:.0f}%")
    c3.metric("平均報酬",   f"{avg_r:+.2f}%")
    c4.metric("平均持有",   f"{avg_h:.1f} 天")
    c5.metric("獲利因子",   f"{pf:.2f}", help="總獲利÷總虧損，>1.5 較穩健")
    c6.metric("最大回撤",   f"{mdd:.1f}pp", help="逐筆累計報酬曲線的最大回落（百分點）")
    if total_pnl_dollar is not None:
        c7.metric("總損益$（估）", f"{total_pnl_dollar:+,.0f}",
                  help="用目前資金設定反推每筆張數估算，不是實際歷史成交量")

    t1, t2, t3 = st.tabs(["📈 累計績效 vs 大盤", "📊 報酬分布", "📋 交易紀錄"])

    # ── Tab 1：累計績效曲線（AI 實際推薦紀錄 vs 0050）─────────────
    with t1:
        st.caption("AI 每筆已平倉交易的累計報酬（逐筆加總）對比同期 0050 買進持有——"
                   "這是判斷「值不值得跟單」的最直接依據")
        try:
            start_d, end_d = df_seq["出場日"].min(), df_seq["出場日"].max()
            with get_session() as s:
                bench_rows = s.execute(text("""
                    SELECT trade_date, close FROM daily_prices
                    WHERE stock_id = '0050' AND close > 0
                      AND trade_date BETWEEN :a AND :b
                    ORDER BY trade_date
                """), {"a": start_d, "b": end_d}).fetchall()
            fig_eq = go.Figure()
            fig_eq.add_trace(go.Scatter(
                x=df_seq["出場日"], y=df_seq["累計%"],
                mode="lines+markers", name="AI 推薦（逐筆累計）",
                line=dict(color="#e74c3c", width=2)))
            if bench_rows:
                bd = [r[0] for r in bench_rows]
                bc = [float(r[1]) for r in bench_rows]
                bench_pct = [(c / bc[0] - 1) * 100 for c in bc]
                fig_eq.add_trace(go.Scatter(
                    x=bd, y=bench_pct, mode="lines", name="0050 買進持有",
                    line=dict(color="#7f8c8d", width=1.5, dash="dot")))
            fig_eq.add_hline(y=0, line_dash="dash", line_color="#95a5a6")
            fig_eq.update_layout(height=380, margin=dict(l=0, r=0, t=30, b=0),
                                 yaxis_title="累計報酬 %",
                                 legend=dict(orientation="h", y=1.1))
            st.plotly_chart(fig_eq, use_container_width=True)
            st.caption("注意：AI 曲線為「逐筆報酬加總」（未含手續費/證交稅，約每筆 -0.49%），"
                       "0050 為區間價格漲幅；兩者口徑略有差異，看趨勢與相對強弱即可。")
        except Exception as e:
            st.warning(f"績效曲線繪製失敗：{e}")

    with t2:
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

    with t3:
        def color_ret(val):
            if isinstance(val, (int, float)):
                return "color: #e74c3c" if val < 0 else "color: #27ae60"
            return ""
        # pandas 2.1+ 將 Styler.applymap 改名為 Styler.map（3.0 移除 applymap）
        styler = df.style
        _elementwise = getattr(styler, "map", None) or styler.applymap
        st.dataframe(
            _elementwise(color_ret, subset=[c for c in ["報酬%", "損益$（估）"] if c in df.columns])
              .format({"進場價": "{:.2f}", "出場價": "{:.2f}", "報酬%": "{:+.2f}%",
                       "損益$（估）": "{:+,.0f}"},
                      na_rep="—"),
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
        from data_pipeline.analysis.daily_digest import digest_age_days
        age = digest_age_days(digest_date)
        title = f"📋 {digest_date} 市場情報彙整（AI 自動生成）" + (f" ⚠️ {age}天前" if age > 0 else "")
        with st.expander(title, expanded=True):
            if age > 0:
                st.warning(f"這是 {age} 天前的彙整，非今日最新（今日資料蒐集可能中斷）")
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
                # 半透明底色：深色/淺色主題都能讀（實心淺色底在深色主題會白字配淺底看不見）
                t = row["異動"]
                if "新增" in str(t) or "加碼" in str(t):
                    return ["background-color: rgba(46, 204, 113, 0.25)"] * len(row)
                if "移除" in str(t) or "減碼" in str(t):
                    return ["background-color: rgba(231, 76, 60, 0.25)"] * len(row)
                return [""] * len(row)

            st.dataframe(
                df_chg.style.apply(_etf_row_style, axis=1)
                      .format({"舊比重%": "{:.2f}", "新比重%": "{:.2f}"}, na_rep="—"),
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
    st.caption("統一旗下主動式ETF換股動態（每日全持股）× 投信連續買超 — 跟著大戶集體操作做波段")

    from data_pipeline.fetchers.uni_etf_fetcher import UNI_ACTIVE_FUNDS
    TRACK_ETFS  = list(UNI_ACTIVE_FUNDS.keys())          # ["00981A", "00403A"]
    ETF_NAMES   = {k: v["name"] for k, v in UNI_ACTIVE_FUNDS.items()}
    inv_days    = st.sidebar.slider("投信回看天數", 10, 30, 15)
    etf_days    = st.sidebar.slider("ETF換股回看天數", 14, 90, 45)
    min_buy_d   = st.sidebar.slider("投信最少買超天數", 2, 7, 3)
    min_total   = st.sidebar.number_input(
        "連買期間累計最少張數", 0, 2000, 100, step=50,
        help="防雜訊：只滿足『連買天數』但累計量太小（例：3天總共只買3張）會被濾掉，"
             "不列入連買清單。此門檻不影響下方『單日大量門檻』分支。")
    min_single  = st.sidebar.number_input("單日大量門檻（張）", 100, 5000, 500, step=100)

    if st.button("🔄 重新整理"):
        st.cache_data.clear()

    # ── 即時查詢（不快取，確保最新）────────────────────────────
    def _query_invest(days, min_days, min_single_v, min_total_v):
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
                        ROUND(SUM(invest_net) / 1000.0)                     AS 淨買超張,
                        ROUND(MAX(invest_net) / 1000.0)                     AS 單日峰值張,
                        MAX(trade_date) FILTER (WHERE invest_net > 0)       AS 最後買超日
                    FROM w
                    GROUP BY stock_id
                    HAVING
                        -- 2026-07-22 修：跟 smart_money.py 同一個bug——要求整段淨買(SUM>0)，
                        -- 否則散落幾天綠K的淨賣股(國巨/群創型態)會被當成投信買超列出。
                        SUM(invest_net) > 0
                        AND (
                            (COUNT(*) FILTER (WHERE invest_net > 0) >= :min_days
                             AND SUM(invest_net) FILTER (WHERE invest_net > 0) >= :min_total * 1000)
                            OR MAX(invest_net) >= :min_s * 1000
                        )
                    ORDER BY 買超天數 DESC, 累計買超張 DESC
                    LIMIT 50
                """), {"days": days, "min_days": min_days, "min_s": min_single_v,
                       "min_total": min_total_v}).fetchall()
            return pd.DataFrame(rows)
        except Exception as e:
            st.error(f"查詢失敗: {e}")
            return pd.DataFrame()

    def _query_etf(etf_ids, days):
        """跨多檔 ETF（統一旗下主動式）彙整加碼/新增，含來源 ETF 名稱。"""
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
                        ec.detected_date                                    AS 偵測日期,
                        -- 英文字母別名必須加引號，否則 PostgreSQL 折成小寫（etf代號）
                        -- 導致頁面 r['ETF代號'] KeyError
                        ec.etf_id                                           AS "ETF代號",
                        ec.etf_name                                         AS "ETF名稱"
                    FROM etf_changes ec
                    WHERE ec.etf_id = ANY(:etf_ids)
                      AND ec.change_type IN ('added','increased')
                      AND ec.detected_date >= CURRENT_DATE - :days * INTERVAL '1 day'
                    ORDER BY ec.detected_date DESC, 權重變化 DESC
                """), {"etf_ids": etf_ids, "days": days}).fetchall()
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

    df_invest = _query_invest(inv_days, min_buy_d, min_single, min_total)
    df_etf    = _query_etf(TRACK_ETFS, etf_days)

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
        st.caption("同時出現在「投信連買」與「統一旗下主動式ETF加碼/新增」的股票——波段勝率最高")

        if not overlap_ids:
            if df_etf.empty:
                st.info(
                    "目前無 ETF 換股記錄可交叉比對（統一主動式 ETF 為每日全持股，"
                    "首次執行後需隔天才會出現換股比對結果）。"
                    "**每日**的主動買賣訊號請先看 **🏦 投信連買排行**。"
                )
            elif df_invest.empty:
                st.info("目前無符合條件的投信買超記錄。")
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
                                f"**📊 {etf_row.get('ETF名稱', '統一ETF')}：** {etf_row['動作']} "
                                f"（{etf_row['偵測日期']}）  \n"
                                f"權重 {etf_row['舊權重']:.2f}% → **{etf_row['新權重']:.2f}%** "
                                f"（+{etf_row['權重變化']:.2f}%）"
                            )

    # ── Tab 2：統一ETF動態 ──────────────────────────────────────
    with tab_etf:
        etf_label = "、".join(f"{eid} {name}" for eid, name in ETF_NAMES.items())
        st.subheader(f"📊 統一旗下主動式ETF — 近{etf_days}日加碼/新增")
        st.caption(f"追蹤：{etf_label}｜持股來源：統一官網每日全持股（非月更前十大），"
                   "換股偵測與大戶操作幾乎同步。")

        if df_etf.empty:
            st.info("近期無換股記錄；下方為最新一期持股明細。")
        else:
            for _, r in df_etf.iterrows():
                weight_delta = r["權重變化"]
                delta_str = f"+{weight_delta:.2f}%" if weight_delta > 0 else f"{weight_delta:.2f}%"
                st.markdown(
                    f"{r['動作']} **{r['股票代號']} {r['股票名稱']}**"
                    f"　權重 {r['舊權重']:.2f}% → **{r['新權重']:.2f}%**（{delta_str}）"
                    f"　📅 {r['偵測日期']}　`{r['ETF代號']} {r['ETF名稱']}`"
                    + ("　⭐" if r["股票代號"] in overlap_ids else "")
                )

        st.markdown("---")
        pick_etf = st.selectbox(
            "查看持股明細",
            TRACK_ETFS,
            format_func=lambda eid: f"{eid} {ETF_NAMES.get(eid, '')}",
        )
        st.subheader(f"最新持股明細（前 20）")
        df_hold = _query_etf_holdings(pick_etf)
        if df_hold.empty:
            st.caption("尚無持股快照（pipeline 執行 ETF 追蹤後自動更新）。")
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
        st.subheader(f"🏦 投信連買排行 — 近{inv_days}日買超≥{min_buy_d}天(累計≥{int(min_total)}張) 或 單日≥{int(min_single)}張")
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


# ══════════════════════════════════════════════════════════════════
#  Page 8：追蹤清單（自選股買點判斷）
# ══════════════════════════════════════════════════════════════════
elif page == "🔖 追蹤清單":
    st.title("🔖 追蹤清單")
    st.caption("可建立多組命名清單（自己的觀察、幫別人記的都分開），"
               "每日 pipeline 自動判斷波段買點（🟢買點浮現 / 🟡接近買點 / ⚪觀望）")

    # ── 清單選擇 / 管理 ─────────────────────────────────────────
    with get_session() as s:
        mylists = s.execute(text("""
            SELECT list_id, list_name,
                   (SELECT COUNT(*) FROM watchlist_items i WHERE i.list_id = w.list_id)
            FROM watchlists w WHERE user_id = :uid ORDER BY list_id
        """), {"uid": USER["user_id"]}).fetchall()

    lc1, lc2, lc3 = st.columns([2, 2, 1])
    list_options = {f"{name}（{cnt} 檔）": lid for lid, name, cnt in mylists}
    pick = lc1.selectbox("目前清單", list(list_options.keys())) if mylists else None
    cur_list_id = list_options[pick] if pick else None

    with lc2.form("new_list", clear_on_submit=True):
        nl_name = st.text_input("新清單名稱", placeholder="例：AI伺服器觀察 / 幫媽媽記的")
        nl_go = st.form_submit_button("➕ 建立清單")
    if nl_go and nl_name.strip():
        with get_session() as s:
            s.execute(text("""
                INSERT INTO watchlists (user_id, list_name) VALUES (:uid, :n)
                ON CONFLICT (user_id, list_name) DO NOTHING
            """), {"uid": USER["user_id"], "n": nl_name.strip()[:50]})
        st.rerun()

    if cur_list_id and lc3.button("🗑 刪除此清單", help="連同清單內全部股票一起刪除"):
        with get_session() as s:
            s.execute(text("DELETE FROM watchlists WHERE list_id = :lid AND user_id = :uid"),
                      {"lid": cur_list_id, "uid": USER["user_id"]})
        st.rerun()

    if cur_list_id is None:
        st.info("先建立一組清單")
        st.stop()

    # ── 加入股票 ────────────────────────────────────────────────
    with st.form("add_watchlist", clear_on_submit=True):
        c1, c2, c3 = st.columns([1, 2, 1])
        w_sid    = c1.text_input("股票代號", placeholder="2330")
        w_note   = c2.text_input("備註（選填）", placeholder="例：等回檔到月線")
        w_target = c3.number_input("目標買入價（選填，0=不設）", min_value=0.0, step=0.5, value=0.0)
        w_submit = st.form_submit_button("➕ 加入此清單")

    if w_submit:
        sid = w_sid.strip()
        with get_session() as s:
            exists = s.execute(text(
                "SELECT stock_name FROM stocks WHERE stock_id = :sid"), {"sid": sid}).fetchone()
        if not exists:
            st.error(f"代號 {sid} 不存在於股票清單，請確認")
        else:
            with get_session() as s:
                s.execute(text("""
                    INSERT INTO watchlist_items (list_id, stock_id, note, target_price)
                    VALUES (:lid, :sid, :note, :tp)
                    ON CONFLICT (list_id, stock_id) DO UPDATE
                        SET note = EXCLUDED.note, target_price = EXCLUDED.target_price
                """), {"lid": cur_list_id, "sid": sid, "note": w_note or None,
                       "tp": w_target if w_target > 0 else None})
            st.success(f"已加入 {sid} {exists[0]}")

    # 立即重新判斷按鈕（平常由每日 pipeline 自動跑）
    col_a, col_b = st.columns([1, 5])
    if col_a.button("⚡ 立即重新判斷"):
        with st.spinner("計算買點訊號中…"):
            from data_pipeline.analysis.watchlist_advisor import evaluate_watchlist
            evaluate_watchlist()
        st.success("已更新")
        st.rerun()

    # 清單表格（目前選中的清單）
    with get_session() as s:
        wrows = s.execute(text("""
            SELECT i.stock_id, st.stock_name, i.added_date, i.note,
                   i.target_price, i.last_signal, i.signal_date,
                   (SELECT d.close FROM daily_prices d
                    WHERE d.stock_id = i.stock_id AND d.close > 0
                    ORDER BY d.trade_date DESC LIMIT 1) AS cur_close,
                   (SELECT d.close FROM daily_prices d
                    WHERE d.stock_id = i.stock_id AND d.close > 0
                      AND d.trade_date >= i.added_date
                    ORDER BY d.trade_date ASC LIMIT 1) AS base_close
            FROM watchlist_items i JOIN stocks st ON st.stock_id = i.stock_id
            WHERE i.list_id = :lid
            ORDER BY i.added_date DESC, i.stock_id
        """), {"lid": cur_list_id}).fetchall()

    if not wrows:
        st.info("此清單是空的，用上方表單加入想追蹤的股票")
    else:
        wrecs = []
        for sid, name, added, note, tp, sig, sig_d, cur, base in wrows:
            cur = float(cur) if cur else None
            base = float(base) if base else None
            chg = (cur / base - 1) * 100 if cur and base else None
            wrecs.append({
                "代號": sid, "名稱": name, "加入日": added,
                "現價": cur, "加入後%": chg,
                "目標價": float(tp) if tp else None,
                "買點判斷": _clean_val(sig) or "（尚未判斷）",
                "判斷日": _clean_val(sig_d) or "—",
                "備註": _clean_val(note) or "",
            })
        dfw = pd.DataFrame(wrecs)

        def signal_style(v):
            v = str(v)
            if v.startswith("🟢"):
                return "background-color: rgba(39,174,96,.15); font-weight: bold"
            if v.startswith("🟡"):
                return "background-color: rgba(241,196,15,.15)"
            return ""

        st.dataframe(
            dfw.style.map(signal_style, subset=["買點判斷"])
               .format({"現價": "{:.2f}", "加入後%": "{:+.2f}%", "目標價": "{:.2f}"},
                       na_rep="—"),
            use_container_width=True, hide_index=True,
        )

        # 移除（從目前清單）
        with st.form("remove_watchlist"):
            c1, c2 = st.columns([3, 1])
            rm_pick = c1.selectbox("移除追蹤",
                                   [f"{r['代號']} {r['名稱']}" for r in wrecs])
            rm_go = c2.form_submit_button("🗑 移除")
        if rm_go and rm_pick:
            rm_sid = rm_pick.split()[0]
            with get_session() as s:
                s.execute(text("""
                    DELETE FROM watchlist_items
                    WHERE list_id = :lid AND stock_id = :sid
                """), {"lid": cur_list_id, "sid": rm_sid})
            st.success(f"已移除 {rm_pick}")
            st.rerun()


# ══════════════════════════════════════════════════════════════════
#  Page：個股分析（隨選、透明分段——決策軌跡概念用在單一股票查詢）
# ══════════════════════════════════════════════════════════════════
elif page == "🔎 個股分析":
    st.title("🔎 個股分析")
    st.caption("輸入一檔股票，逐段收集大盤位階、投信外資買賣超、相關新聞，"
               "最後由 AI 綜合判讀——過程透明，比照決策軌跡的分段概念，"
               "看得到每個結論引用了哪些具體數據。")

    with get_session() as _s:
        _wl_options = pd.read_sql(text("""
            SELECT DISTINCT i.stock_id, st.stock_name
            FROM watchlist_items i
            JOIN watchlists w ON w.list_id = i.list_id AND w.user_id = :uid
            JOIN stocks st ON st.stock_id = i.stock_id
            ORDER BY i.stock_id
        """), _s.bind, params={"uid": USER["user_id"]})

    ic1, ic2, ic3 = st.columns([1.2, 2, 1])
    _wl_pick = ic2.selectbox(
        "從追蹤清單挑選（或直接在左邊輸入代號）",
        [""] + [f"{r.stock_id} {r.stock_name}" for r in _wl_options.itertuples()],
    )
    _default_sid = _wl_pick.split()[0] if _wl_pick else ""
    sa_sid = ic1.text_input("股票代號", value=_default_sid, placeholder="2330")
    sa_go = ic3.button("🔍 開始分析", use_container_width=True)

    if sa_go and sa_sid.strip():
        with st.spinner(f"分析 {sa_sid.strip()} 中（背景資料 + AI 綜合判讀，約 10~20 秒）…"):
            from agent.stock_analysis import analyze_stock
            st.session_state["sa_result"] = analyze_stock(sa_sid.strip())
            st.session_state["sa_sid"] = sa_sid.strip()

    _result = st.session_state.get("sa_result")
    if _result is None:
        st.info("輸入股票代號並按「開始分析」")
        st.stop()
    if not _result["stages"].get("stock_context") or _result["stages"]["stock_context"]["summary"].startswith("查無"):
        st.error(_result["stages"]["stock_context"]["summary"])
        st.stop()

    st.markdown(_DT_CSS, unsafe_allow_html=True)
    _st = _result["stages"]

    def _sa_stage(name):
        s = _st.get(name)
        return (s or {}).get("payload") or {}, (s or {}).get("summary") or ""

    def _sa_meta(name):
        s = _st.get(name) or {}
        if not s.get("model_calls"):
            return ""
        return (f"<span class='dt-num'>詞元 {s.get('tokens_in', 0):,}+{s.get('tokens_out', 0):,}</span>")

    colA, colB, colC, colD = st.columns([1.05, 1.15, 1.1, 1.3], gap="medium")

    # ── 01 股票背景 ──────────────────────────────────────────────
    with colA:
        st.markdown("<div class='dt-col-title'>📊 股票背景 <span class='dt-badge'>01</span></div>",
                    unsafe_allow_html=True)
        _pl, _sum = _sa_stage("stock_context")
        basic, trend, rev_yoy = _pl.get("basic", {}), _pl.get("trend", {}), _pl.get("rev_yoy")
        _chips = []
        if trend.get("rs20") is not None:
            # 2026-07-22：原本一律綠chip寫「RS X 百分位」，國巨rs20=0.0124會顯示成綠色
            # 「RS 1 百分位」誤導成強勢。改成標強弱+依強弱給色（弱給橘色警示）。
            # 變數名不可用 _st！那是外層存 stages 的字典（_st = _result["stages"]），
            # 命名撞到會把它蓋成字串，後面 _sa_stage() 的 _st.get() 直接 AttributeError
            # 炸掉整頁——2026-07-22 上線後實際踩到過。
            _rs = trend["rs20"] * 100
            _rs_label = "極弱" if _rs < 20 else ("偏弱" if _rs < 40 else ("中等" if _rs < 60 else ("偏強" if _rs < 80 else "極強")))
            _rs_cls = "g" if _rs >= 60 else ("o" if _rs < 40 else "")
            _chips.append(f"<span class='dt-chip {_rs_cls}'>相對強度 贏過{_rs:.0f}%（{_rs_label}）</span>")
        if trend.get("stack_days"):
            _chips.append(f"<span class='dt-chip'>多頭排列 {trend['stack_days']:.0f} 日</span>")
        else:
            _chips.append("<span class='dt-chip o'>非多頭排列</span>")
        if rev_yoy is not None:
            _chips.append(f"<span class='dt-chip'>營收 {rev_yoy:+.0f}%</span>")
        # 乖離月線：跌破月線一定幅度＝接刀警示
        _c, _m20 = basic.get("close"), basic.get("ma20")
        _dev = ((_c - _m20) / _m20 * 100) if (_c and _m20) else None
        _dev_html = ""
        if _dev is not None:
            _dev_cls = "o" if _dev <= -10 else ("g" if _dev >= 0 else "")
            _dev_html = f"<span class='dt-chip {_dev_cls}'>乖離月線 {_dev:+.0f}%</span>"
        _body = (f"收盤 {basic.get('close')}　{basic.get('change_pct') or 0:+.2f}%<br>"
                 f"RSI {basic.get('rsi14') or 0:.1f}｜MACD柱"
                 f"{'正' if (basic.get('macd_hist') or 0) > 0 else '負'}<br>"
                 + "".join(_chips) + _dev_html)
        st.markdown(_dt_card("dt-ok", "📦", f"{basic.get('stock_name','')}（{basic.get('industry','')}）",
                             _body), unsafe_allow_html=True)

    # ── 02 大盤與籌碼 ────────────────────────────────────────────
    with colB:
        st.markdown("<div class='dt-col-title'>🌐 大盤與籌碼 <span class='dt-badge'>02</span></div>",
                    unsafe_allow_html=True)
        _pl_r, _ = _sa_stage("market_regime")
        _regime_ok = _pl_r.get("ok")
        _body_r = (f"{_pl_r.get('stock_id')} {_pl_r.get('close', 0):.2f} vs MA60 {_pl_r.get('ma60', 0):.2f}"
                   if _regime_ok else "查無資料，保守視為多頭")
        st.markdown(_dt_card("dt-ok" if _pl_r.get("bull") else "dt-bad",
                             "🐂" if _pl_r.get("bull") else "🐻",
                             f"大盤{'多頭' if _pl_r.get('bull') else '空頭'}", _body_r),
                    unsafe_allow_html=True)

        _pl_i, _ = _sa_stage("institutional_flow")
        if _pl_i.get("ok"):
            _body_i = (f"投信連買 <b>{_pl_i['invest_streak_days']}</b> 日"
                       f"（累計 {_pl_i['invest_streak_lots']:+.0f} 張）<br>"
                       f"外資連買 <b>{_pl_i['foreign_streak_days']}</b> 日"
                       f"（累計 {_pl_i['foreign_streak_lots']:+.0f} 張）"
                       f"<div class='dt-num'>最新資料日 {_pl_i.get('latest_date')}</div>")
            st.markdown(_dt_card("dt-info", "🏦", "投信/外資買賣超", _body_i), unsafe_allow_html=True)
        else:
            st.markdown(_dt_card("dt-warn", "🏦", "投信/外資買賣超", "<i>查無法人買賣超資料</i>"),
                        unsafe_allow_html=True)

    # ── 03 相關新聞 ──────────────────────────────────────────────
    with colC:
        st.markdown("<div class='dt-col-title'>📰 相關新聞 <span class='dt-badge'>03</span></div>",
                    unsafe_allow_html=True)
        _pl_n, _ = _sa_stage("news_context")
        _news = _pl_n.get("news") or []
        if _news:
            for n in _news:
                _sent = {"positive": "dt-ok", "negative": "dt-bad"}.get(n.get("sentiment"), "dt-info")
                _link = f"<a href='{n['url']}' target='_blank'>原文</a>" if n.get("url") else ""
                st.markdown(_dt_card(_sent, "📰", str(n.get("date")),
                                     f"{_dt_esc(n.get('title'))}<br>{_link}"),
                            unsafe_allow_html=True)
        else:
            st.markdown(_dt_card("dt-warn", "📰", "相關新聞", "<i>近30日查無相關報導</i>"),
                        unsafe_allow_html=True)

    # ── 04 AI 綜合判讀 ───────────────────────────────────────────
    with colD:
        st.markdown("<div class='dt-col-title'>🤖 AI 綜合判讀 <span class='dt-badge'>04</span></div>",
                    unsafe_allow_html=True)
        _pl_s, _ = _sa_stage("synthesis")
        _parsed = _pl_s.get("parsed")
        if not _parsed:
            st.markdown(_dt_card("dt-bad", "❌", "AI 綜合判讀失敗",
                                 "本次 LLM 輸出無法解析，上面幾段資料仍完整可參考"),
                        unsafe_allow_html=True)
        else:
            _verdict_icon = {"positive": "🟢", "neutral": "🟡", "negative": "🔴"}.get(_parsed.get("verdict"), "🟡")
            st.markdown(f"<div class='dt-verdict'>{_verdict_icon} <b>{_dt_esc(_parsed.get('verdict'))}</b>"
                        f"　{_dt_esc(_parsed.get('verdict_reason'))}<br>"
                        f"{_dt_esc(_parsed.get('summary'))}</div>", unsafe_allow_html=True)

            # 關鍵價位（程式算好的支撐/壓力，權威數字；LLM的key_levels/invalidation當解讀）
            _pl_lv, _ = _sa_stage("price_levels")
            if _pl_lv.get("ok"):
                def _lv_line(items, arrow):
                    return "<br>".join(
                        f"{arrow} <b>{it['price']:.2f}</b>（{it['dist_pct']:+.1f}%）"
                        f"<span class='dt-num'>{_dt_esc(it['label'])}</span>" for it in items) or "<i>—</i>"
                _body_lv = (f"<div style='color:#d9a441'>上方壓力</div>{_lv_line(_pl_lv.get('resistances') or [], '⬆')}"
                            f"<div style='color:#34a06b;margin-top:.4rem'>下方支撐</div>{_lv_line(_pl_lv.get('supports') or [], '⬇')}")
                st.markdown(_dt_card("dt-info", "📏", f"關鍵價位（現價 {_pl_lv.get('close'):.2f}）", _body_lv),
                            unsafe_allow_html=True)
            _klev = _parsed.get("key_levels") or []
            if _klev:
                st.markdown(_dt_card("dt-info", "🎯", "該留意的價位（AI 解讀）",
                                     "<br>".join(f"・{_dt_esc(k)}" for k in _klev)), unsafe_allow_html=True)
            if _parsed.get("invalidation"):
                st.markdown(_dt_card("dt-warn", "🔄", "什麼情況會推翻此判斷",
                                     _dt_esc(_parsed.get("invalidation"))), unsafe_allow_html=True)

            for p in _parsed.get("bull_points") or []:
                _ev = "".join(f"<span class='dt-chip g'>{_dt_esc(e)}</span>" for e in (p.get("evidence_fields") or []))
                st.markdown(_dt_card("dt-ok", "🐂", "多方論點", f"{_dt_esc(p.get('point'))}<br>{_ev}"),
                            unsafe_allow_html=True)
            for p in _parsed.get("bear_points") or []:
                _ev = "".join(f"<span class='dt-chip o'>{_dt_esc(e)}</span>" for e in (p.get("evidence_fields") or []))
                st.markdown(_dt_card("dt-bad", "🐻", "空方論點", f"{_dt_esc(p.get('point'))}<br>{_ev}"),
                            unsafe_allow_html=True)
            _gaps = _parsed.get("data_gaps") or []
            if _gaps:
                st.markdown(_dt_card("dt-warn", "⚠️", "資料缺口",
                                     "<br>".join(_dt_esc(g) for g in _gaps)),
                            unsafe_allow_html=True)
            _gflags = _pl_s.get("grounding_flags") or []
            if _gflags:
                _body = "<br>".join(
                    f"「{_dt_esc(g.get('point'))}」→ 查無對應：{_dt_esc('、'.join(str(v) for v in g.get('numbers') or []))}"
                    for g in _gflags)
                st.markdown(_dt_card("dt-bad", "🔍", "引用驗證：以下數字在系統資料裡找不到對應（可能是 AI 掰的，別採信）",
                                     _body), unsafe_allow_html=True)
        st.caption(_sa_meta("synthesis") or "")
        st.caption(f"run_id: {_result['run_id'][:8]}…（完整紀錄可查 execution_log，kind=stock_analysis）")


# ══════════════════════════════════════════════════════════════════
#  Page 8.5：練習軌——每日 20 盲盒（純量化，不進 LLM）
# ══════════════════════════════════════════════════════════════════
elif page == "🎯 練習軌":
    st.title("🎯 練習軌：每日 20 盲盒")
    st.caption("純量化篩選，完全不經過 LLM——用來練「波段操作、40%勝率也能小賠大賺」的量化心態，"
               "不是給你抄的答案。建議流程：只看代號進 TradingView，只開 20MA 和成交量，"
               "隱藏新聞與籌碼，自己找「20MA上方橫盤5-10天、今天帶量紅K突破箱型」的股票；"
               "停損守突破K棒低點或月線（取低者）、停利沿20MA抱到跌破。")

    from agent.stock_selector import get_practice_candidates
    if st.button("🔄 重新整理今日清單", use_container_width=False):
        st.session_state.pop("practice_candidates", None)
    if "practice_candidates" not in st.session_state:
        with st.spinner("篩選中…"):
            st.session_state["practice_candidates"] = get_practice_candidates(top_n=20)
    _pc = st.session_state["practice_candidates"]

    if _pc is None or _pc.empty:
        st.warning("今日無符合門檻的候選股票（收盤≥15元、近5日均成交金額≥2億、站上月線）。")
        st.stop()

    _show = _pc.copy()
    _show.insert(0, "排名", range(1, len(_show) + 1))
    _show["投信連買(日)"] = _show.get("invest_streak", 0).fillna(0).astype(int)
    _show["多頭排列(日)"] = _show.get("stack_days", 0).fillna(0).astype(int)
    _show["月營收年增%"] = _show.get("rev_yoy").round(1)
    _cols = ["排名", "stock_id", "stock_name", "industry", "close",
             "投信連買(日)", "多頭排列(日)", "月營收年增%", "score"]
    _cols = [c for c in _cols if c in _show.columns]
    st.dataframe(
        _show[_cols].rename(columns={"stock_id": "代號", "stock_name": "名稱",
                                     "industry": "產業", "close": "收盤", "score": "量化分數"}),
        use_container_width=True, hide_index=True,
    )

    _hard = _pc.attrs.get("hard_excluded") or []
    if _hard:
        with st.expander(f"⚠️ {len(_hard)} 檔被硬否決規則排除（乖離月線過遠 / 帶量長上引線）"):
            for h in _hard:
                st.caption(f"{h['stock_id']} {h['stock_name']}：{h['hard_veto_reason']}")

    st.caption("硬門檻：收盤≥15元、近5日均成交金額≥2億/日、站上月線(MA20)、RSI 45~88。"
               "評分只看三個純量化因子：投信連買×2.5、多頭排列天數×1.5、月營收年增>20%×2.0——"
               "跟 AI 軌（決策軌跡頁）用不同權重，兩軌互不影響。")


# ══════════════════════════════════════════════════════════════════
#  Page 9：族群輪動（細分族群 + 龍頭股）
# ══════════════════════════════════════════════════════════════════
elif page == "🔥 族群輪動":
    st.title("🔥 族群輪動")
    st.caption("細分族群（記憶體/AI伺服器/散熱…）+ 龍頭股表現，一眼看出今天輪到誰")

    from data_pipeline.analysis.group_momentum import (
        calc_group_momentum, group_members_detail, rotation_alerts,
    )

    @st.cache_data(ttl=300)
    def load_group_momentum():
        return calc_group_momentum()

    if st.button("🔄 重新整理"):
        load_group_momentum.clear()

    gdf = load_group_momentum()
    if gdf.empty:
        st.info("尚無族群資料（需先執行 migration 09 並確認 daily_prices 有當日資料）")
    else:
        trade_dt = gdf["trade_date"].iloc[0]
        st.caption(f"資料日期：{trade_dt}")

        # 輪動提示
        alerts = rotation_alerts(gdf)
        if alerts:
            for a in alerts:
                st.warning(a)
        else:
            st.info("今日無明顯族群輪動（龍頭漲>2% 且 6 成成員上漲才提示）")

        # 熱度排行（台股慣例：紅漲綠跌）
        plot_df = gdf.sort_values("avg_change_pct")
        colors = ["#e74c3c" if v >= 0 else "#27ae60" for v in plot_df["avg_change_pct"]]
        fig = go.Figure(go.Bar(
            x=plot_df["avg_change_pct"], y=plot_df["group_name"],
            orientation="h", marker_color=colors,
            text=[f"{v:+.2f}%" for v in plot_df["avg_change_pct"]],
            textposition="outside",
        ))
        fig.update_layout(
            title="各族群當日平均漲跌（依熱度分數排序見下表）",
            height=420, margin=dict(l=0, r=40, t=40, b=0),
            xaxis_title="平均漲跌 %",
        )
        st.plotly_chart(fig, use_container_width=True)

        # 排行表
        show = gdf[["group_name", "avg_change_pct", "rising", "total",
                    "leader_names", "leader_change_pct", "inst_net_lots",
                    "momentum_score"]].rename(columns={
            "group_name": "族群", "avg_change_pct": "平均漲跌%",
            "rising": "上漲家數", "total": "總家數",
            "leader_names": "龍頭", "leader_change_pct": "龍頭漲跌%",
            "inst_net_lots": "法人買超(張)", "momentum_score": "熱度分",
        })
        st.dataframe(
            show.style.format(na_rep="—", formatter={"平均漲跌%": "{:+.2f}%", "龍頭漲跌%": "{:+.2f}%",
                               "法人買超(張)": "{:+,d}", "熱度分": "{:.3f}"}),
            use_container_width=True, hide_index=True,
        )

        # 族群明細
        st.markdown("---")
        pick_name = st.selectbox("查看族群成員明細", gdf["group_name"].tolist())
        pick_code = gdf[gdf["group_name"] == pick_name]["group_code"].iloc[0]
        det = group_members_detail(pick_code)
        if det.empty:
            st.info("此族群今日無成員資料")
        else:
            det["龍頭"] = det["龍頭"].map(lambda x: "👑" if x else "")
            for c in ("漲跌%", "收盤", "RSI", "近5日法人(張)"):
                det[c] = pd.to_numeric(det[c], errors="coerce")
            det["成交量"] = pd.to_numeric(det["成交量"], errors="coerce").fillna(0).astype("int64")
            st.dataframe(
                det.style.format({"漲跌%": "{:+.2f}%", "收盤": "{:.2f}",
                                  "RSI": "{:.0f}", "近5日法人(張)": "{:+,.0f}",
                                  "成交量": "{:,d}"}, na_rep="—"),
                use_container_width=True, hide_index=True,
            )


# ══════════════════════════════════════════════════════════════════
#  Page：決策軌跡（execution log —— AI 為什麼選這檔 + 20 分鐘花在哪）
#  規格：docs/SPEC_PIPELINE_IMPROVEMENTS.md Phase A
# ══════════════════════════════════════════════════════════════════
elif page == "🔍 決策軌跡":
    st.title("🔍 決策軌跡")
    st.caption("每次 pipeline 的完整決策過程：資料 → 因子篩選 → 多空辯論 → 裁決 → 風控 → 委託。"
               "每段附耗時與 LLM 用量；保留 180 天自動輪替。")

    _STAGE_LABEL = {
        "data_ingest":     "📥 資料補齊＋技術指標",
        "quality_gate":    "🧪 資料品質檢查",
        "market_intel":    "📰 市場情報（新聞/YT/ETF/聰明資金）",
        "sector_momentum": "🔥 族群輪動熱度",
        "fills":           "✅ 開盤成交回帳（昨日掛單）",
        "orders_exits":    "🔔 出場檢查 → 掛賣單",
        "risk_gate":       "🛡️ 風控閘門（市場濾網/部位上限）",
        "factor_screen":   "🎯 因子篩選（為什麼是這些候選）",
        "debate_bull":     "🐂 多方研究員",
        "debate_bear":     "🐻 空方研究員",
        "judge":           "⚖️ 首席投資長裁決",
        "orders_entries":  "🛒 掛買單（明日開盤）",
        "advisors":        "📦 手動持倉＋追蹤清單建議",
        "notify":          "📨 Telegram 推播",
    }

    with get_session() as _s:
        _runs = pd.read_sql(text("""
            SELECT run_id, MIN(started_at) AS run_start,
                   SUM(duration_ms) AS total_ms,
                   SUM(model_calls) AS llm_calls,
                   SUM(tokens_in) AS tin, SUM(tokens_out) AS tout,
                   COUNT(*) FILTER (WHERE status = 'failed') AS failed
            FROM execution_log
            WHERE kind = 'pipeline'
            GROUP BY run_id ORDER BY run_start DESC LIMIT 30
        """), _s.bind)

    if _runs.empty:
        st.info("還沒有決策軌跡紀錄——下一次 pipeline 執行後就會出現。")
        st.stop()

    _runs["label"] = _runs.apply(
        lambda r: f"{pd.Timestamp(r['run_start']).tz_convert('Asia/Taipei'):%Y-%m-%d %H:%M}"
                  f"（{(r['total_ms'] or 0)/60000:.1f} 分鐘"
                  + (f"，⚠️{int(r['failed'])} 段失敗" if r["failed"] else "") + "）",
        axis=1)
    _sel = st.selectbox("選擇一次執行", _runs.index,
                        format_func=lambda i: _runs.loc[i, "label"])
    _run = _runs.loc[_sel]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("總耗時", f"{(_run['total_ms'] or 0)/60000:.1f} 分")
    c2.metric("LLM 呼叫", int(_run["llm_calls"] or 0))
    c3.metric("Tokens (in/out)", f"{int(_run['tin'] or 0):,} / {int(_run['tout'] or 0):,}")
    c4.metric("失敗段", int(_run["failed"] or 0))

    with get_session() as _s:
        _stages = pd.read_sql(text("""
            SELECT stage, seq, started_at, duration_ms, model_calls,
                   tokens_in, tokens_out, sources, summary, payload, status, error_msg
            FROM execution_log WHERE run_id = :rid ORDER BY seq
        """), _s.bind, params={"rid": _run["run_id"]})

    # ── 卡片樣式（狀態色一律搭配圖示與文字標籤，不單靠顏色傳達）────────
    st.markdown("""
    <style>
    .dt-card{background:rgba(128,138,160,.08);border:1px solid rgba(128,138,160,.22);
      border-radius:10px;padding:.65rem .8rem;margin-bottom:.55rem;font-size:.85rem;line-height:1.5}
    .dt-ok{border-left:4px solid #34a06b}
    .dt-warn{border-left:4px solid #d9a441}
    .dt-bad{border-left:4px solid #d05252}
    .dt-info{border-left:4px solid #5b84d6}
    .dt-head{font-weight:700;margin-bottom:.3rem;display:flex;justify-content:space-between;gap:.5rem;align-items:baseline}
    .dt-chip{display:inline-block;padding:.02rem .5rem;border-radius:99px;font-size:.72rem;
      background:rgba(91,132,214,.16);border:1px solid rgba(91,132,214,.4);margin:0 .18rem .18rem 0}
    .dt-chip.g{background:rgba(52,160,107,.14);border-color:rgba(52,160,107,.45)}
    .dt-chip.o{background:rgba(217,164,65,.14);border-color:rgba(217,164,65,.5)}
    .dt-num{font-size:.72rem;opacity:.65;white-space:nowrap}
    .dt-scroll{max-height:460px;overflow-y:auto;white-space:pre-wrap}
    .dt-col-title{font-weight:800;font-size:.95rem;margin:.15rem 0 .5rem 0}
    .dt-badge{background:rgba(91,132,214,.3);border-radius:6px;padding:0 .4rem;font-size:.72rem;font-weight:700}
    .dt-verdict{background:rgba(52,160,107,.10);border:1px solid rgba(52,160,107,.45);
      border-radius:12px;padding:.8rem .9rem;margin-bottom:.6rem;font-size:.9rem;line-height:1.55}
    </style>""", unsafe_allow_html=True)

    import html as _htmlmod

    def _esc(t):
        return _htmlmod.escape(str(t)).replace("\n", "<br>")

    def _i(v):
        return 0 if v is None or pd.isna(v) else int(v)

    def _stage_row(name):
        _r = _stages[_stages["stage"] == name]
        if _r.empty:
            return None, {}
        _row2 = _r.iloc[0]
        _pl2 = _row2["payload"]
        if isinstance(_pl2, str):
            try:
                _pl2 = json.loads(_pl2)
            except Exception:
                _pl2 = {}
        return _row2, (_pl2 or {})

    def _llm_meta(_row2):
        if _row2 is None or not _i(_row2["model_calls"]):
            return ""
        return (f"<span class='dt-num'>詞元 {_i(_row2['tokens_in']):,}+{_i(_row2['tokens_out']):,}"
                f" ｜ {_i(_row2['duration_ms'])/1000:.1f}s</span>")

    def _card(cls, icon, title, body_html, meta=""):
        return (f"<div class='dt-card {cls}'><div class='dt-head'>"
                f"<span>{icon} {title}</span>{meta}</div>{body_html}</div>")

    def _status_card(name, icon, title):
        _row2, _ = _stage_row(name)
        if _row2 is None:
            return _card("dt-warn", "⏭️", title, "<i>本次未執行此段</i>")
        if _row2["status"] == "failed":
            return _card("dt-bad", "❌", f"{title}（失敗）", _esc(_row2["error_msg"] or "失敗"))
        return _card("dt-ok", icon, title, _esc(_row2["summary"] or "完成"),
                     f"<span class='dt-num'>{_i(_row2['duration_ms'])/1000:.1f}s</span>")

    tab_flow, tab_eng = st.tabs(["🧭 決策流程", "🛠 工程視圖（逐段耗時／原始紀錄）"])

    # ═══ 決策流程：資料與候選 → 多空辯論 → 首席裁決 → 結論與行動 ═══
    with tab_flow:
        colA, colB, colC, colD = st.columns([1.0, 1.3, 1.3, 1.05], gap="medium")

        # ── 01 資料與候選 ──────────────────────────────────────────
        with colA:
            st.markdown("<div class='dt-col-title'>📥 資料與候選 <span class='dt-badge'>01</span></div>",
                        unsafe_allow_html=True)
            st.markdown(_status_card("data_ingest", "📦", "資料補齊＋技術指標"), unsafe_allow_html=True)
            st.markdown(_status_card("market_intel", "📰", "市場情報"), unsafe_allow_html=True)

            # Phase B 資料品質層：規則驗證器 + 來源信心分數
            _rq, _pq = _stage_row("quality_gate")
            if _rq is not None:
                _srcs_q = (_rq["sources"] if isinstance(_rq["sources"], list)
                          else json.loads(_rq["sources"])) if _rq["sources"] is not None else []
                _chips_q = "".join(
                    f"<span class='dt-chip {'g' if s.get('confidence', 1) >= 0.8 else 'o'}'>"
                    f"{_esc(s.get('source'))} 信心 {s.get('confidence')}</span>"
                    for s in _srcs_q)
                _discs = (_pq.get("discrepancies") or [])
                _body_q = _esc(_rq["summary"] or "") + (f"<br>{_chips_q}" if _chips_q else "")
                for _d in _discs:
                    _body_q += (f"<div class='dt-num'>⚠ {_esc(_d.get('check_name'))}"
                               f"（{_esc(_d.get('stock_id') or '整體')}）："
                               f"預期{_esc(_d.get('expected'))}，實際{_esc(_d.get('actual'))}"
                               f"<br>　{_esc(_d.get('note'))}</div>")
                st.markdown(_card("dt-bad" if any(d.get("severity") == "error" for d in _discs)
                                  else ("dt-warn" if _discs else "dt-ok"),
                                  "🧪", "資料品質檢查", _body_q,
                                  f"<span class='dt-num'>{_i(_rq['duration_ms'])/1000:.1f}s</span>"),
                            unsafe_allow_html=True)

            _row_fs, _pl_fs = _stage_row("factor_screen")
            if _row_fs is None:
                st.markdown(_card("dt-warn", "⏭️", "因子篩選",
                                  "<i>本次未進行選股（週末模式或流程未達此段）</i>"),
                            unsafe_allow_html=True)
            else:
                _hot = "".join(f"<span class='dt-chip'>{_esc(h)}</span>"
                               for h in (_pl_fs.get("hot_sectors") or []))
                st.markdown(_card("dt-ok", "🎯", "因子篩選",
                                  _esc(_row_fs["summary"] or "") + (f"<br>{_hot}" if _hot else ""),
                                  f"<span class='dt-num'>{_i(_row_fs['duration_ms'])/1000:.1f}s</span>"),
                            unsafe_allow_html=True)
                _all_cands = _pl_fs.get("top_candidates") or []
                # 2026-07-22修正：原本只顯示前8檔，但完整candidates_text是把全部（通常
                # 20檔）都餵給多方/空方/裁決三個LLM——只顯示前8會讓使用者以為排名較後面
                # 的候選「不在資料裡」，之前玉山金(排名14)出現在裁決結果卻不在這裡顯示，
                # 讓使用者誤以為是幻覺，其實它從頭到尾都在候選資料裡，只是沒被UI列出來。
                # 改成全部顯示、包在可捲動區塊裡，跟多空辯論欄位一致的呈現方式。
                _cand_cards = []
                for _c in _all_cands:
                    _chips = []
                    if _c.get("rs20") is not None:
                        _chips.append(f"<span class='dt-chip g'>RS {float(_c['rs20'])*100:.0f} 百分位</span>")
                    if _c.get("stack_days"):
                        _chips.append(f"<span class='dt-chip'>多頭排列 {float(_c['stack_days']):.0f} 日</span>")
                    if _c.get("rev_yoy") is not None:
                        _chips.append(f"<span class='dt-chip'>營收 {float(_c['rev_yoy']):+.0f}%</span>")
                    if _c.get("invest_streak"):
                        _chips.append(f"<span class='dt-chip'>投信連買 {float(_c['invest_streak']):.0f} 日</span>")
                    _cand_cards.append(_card("dt-info", "",
                                      f"{_esc(_c.get('stock_id'))} {_esc(_c.get('stock_name') or '')}",
                                      "".join(_chips),
                                      f"<span class='dt-badge'>{float(_c.get('score') or 0):.2f} 分</span>"))
                st.markdown(f"<div class='dt-scroll'>{''.join(_cand_cards)}</div>", unsafe_allow_html=True)
                st.caption(f"※ 共 {len(_all_cands)} 檔候選（多空辯論與裁決看到的是完整這份清單，非只有上面顯示的）。"
                           "來源信心分數見上方「資料品質檢查」卡；固定公式(1-近30日觸發次數/10)，非學習模型")

        # ── 02 多空辯論（結構化優先，散文 fallback）────────────────
        with colB:
            st.markdown("<div class='dt-col-title'>🥊 多空辯論 <span class='dt-badge'>02</span></div>",
                        unsafe_allow_html=True)
            _rb, _pb = _stage_row("debate_bull")
            if _rb is None:
                st.markdown(_card("dt-warn", "⏭️", "多方研究員", "<i>流程未達此段</i>"),
                            unsafe_allow_html=True)
            else:
                _parsed = _pb.get("parsed")
                if _parsed and _parsed.get("picks"):
                    _segs = []
                    for _p3 in _parsed["picks"]:
                        _ev = "".join(f"<span class='dt-chip g'>{_esc(e3)}</span>"
                                      for e3 in (_p3.get("evidence_fields") or []))
                        _pre = (f"<div class='dt-num'>自辯：{_esc(_p3.get('preempt_rebuttal'))}</div>"
                                if _p3.get("preempt_rebuttal") else "")
                        _segs.append(f"<div style='margin-bottom:.55rem'>"
                                     f"<b>{_esc(_p3.get('stock_id'))} {_esc(_p3.get('stock_name') or '')}</b>"
                                     f"<div>{_esc(_p3.get('thesis') or '')}</div>{_ev}{_pre}</div>")
                    _body = f"<div class='dt-scroll'>{''.join(_segs)}</div>"
                elif _pb.get("text"):
                    _body = f"<div class='dt-scroll'>{_esc(_pb.get('text'))}</div>"
                else:
                    _body = "<i>本段無輸出（呼叫失敗，裁決退化為單次模式）</i>"
                st.markdown(_card("dt-ok", "🐂", "多方研究員", _body, _llm_meta(_rb)),
                            unsafe_allow_html=True)
            _rr, _pr = _stage_row("debate_bear")
            if _rr is None:
                st.markdown(_card("dt-warn", "⏭️", "空方研究員", "<i>流程未達此段</i>"),
                            unsafe_allow_html=True)
            else:
                _parsed = _pr.get("parsed")
                if _parsed and "vetoes" in _parsed:
                    _segs = []
                    for _v3 in _parsed.get("vetoes") or []:
                        _sev = _v3.get("severity")
                        _chip = ("<span class='dt-chip o'>🚫 VETO</span>" if _sev == "veto"
                                 else "<span class='dt-chip'>⚠ 提醒</span>")
                        _segs.append(f"<div style='margin-bottom:.55rem'>{_chip} "
                                     f"<b>{_esc(_v3.get('stock_id'))} {_esc(_v3.get('stock_name') or '')}</b>"
                                     f"<div>{_esc(_v3.get('reason') or '')}</div></div>")
                    for _mc in _parsed.get("market_concerns") or []:
                        _segs.append(f"<div class='dt-num'>整體：{_esc(_mc)}</div>")
                    _body = f"<div class='dt-scroll'>{''.join(_segs) or '<i>空方無異議</i>'}</div>"
                elif _pr.get("text"):
                    _body = f"<div class='dt-scroll'>{_esc(_pr.get('text'))}</div>"
                else:
                    _body = "<i>本段無輸出（呼叫失敗，裁決退化為單次模式）</i>"
                st.markdown(_card("dt-bad", "🐻", "空方研究員", _body, _llm_meta(_rr)),
                            unsafe_allow_html=True)

        # ── 03 首席裁決 ────────────────────────────────────────────
        with colC:
            st.markdown("<div class='dt-col-title'>⚖️ 首席裁決 <span class='dt-badge'>03</span></div>",
                        unsafe_allow_html=True)
            _rj, _pj = _stage_row("judge")
            if _rj is None:
                st.markdown(_card("dt-warn", "⏭️", "裁決", "<i>流程未達此段</i>"), unsafe_allow_html=True)
            else:
                _mode = "已權衡多空辯論" if _pj.get("debate_used") else "單次模式（辯論失敗退化）"
                st.markdown(_card("dt-ok" if _pj.get("debate_used") else "dt-warn", "⚖️",
                                  f"裁決（{_mode}）", _esc(_rj["summary"] or ""), _llm_meta(_rj)),
                            unsafe_allow_html=True)
                _ga = _pj.get("guardrail_actions") or []
                if _ga:
                    _body = "<br>".join(f"{'🚫' if a.get('action') == '剔除' else '↪️'} "
                                        f"{_esc(a.get('action'))} {_esc(a.get('stock_id'))}：{_esc(a.get('why'))}"
                                        for a in _ga)
                    st.markdown(_card("dt-bad", "🛡️", "裁決問責 guardrail（程式攔截，非 LLM）", _body),
                                unsafe_allow_html=True)
                for _rc in ((_pj.get("result") or {}).get("recommendations") or []):
                    _sig = "".join(f"<span class='dt-chip g'>{_esc(s2)}</span>"
                                   for s2 in (_rc.get("key_signals") or []))
                    _risk = (f"<div><span class='dt-chip o'>⚠ {_esc(_rc.get('risk_note'))}</span></div>"
                             if _rc.get("risk_note") else "")
                    _gf = _rc.get("grounding_flags") or []
                    _risk += (f"<div><span class='dt-chip o'>🔍 引用可疑數字（候選資料查無對應）："
                              f"{_esc('、'.join(str(v) for v in _gf))}</span></div>" if _gf else "")
                    if _rc.get("bypassed_backups"):
                        _risk += ("<div><span class='dt-chip o'>🧨 未經多方主張，且裁決跳過自己列的"
                                  "backups直接從候選資料另挑（判斷前後不一致，多留意）</span></div>")
                    elif _rc.get("not_debated"):
                        _risk += ("<div><span class='dt-chip o'>⚡ 未經多方主張，"
                                  "由裁決直接從候選資料選出（少一層辯論檢視）</span></div>")
                    # 裁決問責：對空方異議的逐條回應（接受/駁回+理由）
                    _oas = ""
                    for _oa in (_rc.get("objections_addressed") or []):
                        if not (_oa.get("objection") or _oa.get("rebuttal")):
                            continue
                        if _oa.get("verdict") == "駁回":
                            _oas += (f"<div class='dt-num'>🗡 空方：{_esc(_oa.get('objection'))}<br>"
                                     f"↩️ <b>駁回</b>：{_esc(_oa.get('rebuttal'))}</div>")
                        else:
                            _oas += (f"<div class='dt-num'>🗡 空方：{_esc(_oa.get('objection'))}"
                                     f"　→ ✅ 接受</div>")
                    st.markdown(_card("dt-info", f"#{_rc.get('rank', '?')}",
                                      f"{_esc(_rc.get('stock_id'))} {_esc(_rc.get('stock_name') or '')}",
                                      _sig + f"<div>{_esc(_rc.get('reason') or '')}</div>" + _risk + _oas),
                                unsafe_allow_html=True)

        # ── 04 結論與行動 ──────────────────────────────────────────
        with colD:
            st.markdown("<div class='dt-col-title'>📨 結論與行動 <span class='dt-badge'>04</span></div>",
                        unsafe_allow_html=True)
            _rj2, _pj2 = _stage_row("judge")
            _ms = (_pj2.get("result") or {}).get("market_summary") if _rj2 is not None else None
            if _ms:
                st.markdown(f"<div class='dt-verdict'>🧭 <b>市場判讀</b><br>{_esc(_ms)}</div>",
                            unsafe_allow_html=True)
            _re2, _pe2 = _stage_row("orders_entries")
            if _re2 is not None:
                _q = _pe2.get("queued") or []
                _body = ("<br>".join(
                    f"🛒 {_esc(q.get('stock_id'))}（訊號價 {float(q.get('signal_price') or 0):.2f}，"
                    f"實際以明日開盤價成交）" for q in _q)
                    if _q else _esc(_re2["summary"] or "無新買單"))
                st.markdown(_card("dt-ok" if _q else "dt-warn", "🛒", "明日開盤買單", _body),
                            unsafe_allow_html=True)
            _rx, _px2 = _stage_row("orders_exits")
            if _rx is not None:
                _xs = _px2.get("exits") or []
                if _xs:
                    _body = "<br>".join(
                        f"🔔 {_esc(x.get('stock_id'))} {_esc(x.get('stock_name') or '')}"
                        f"（{float(x.get('return_pct') or 0):+.1f}%，{_esc(x.get('reason') or '')}）"
                        for x in _xs)
                    st.markdown(_card("dt-warn", "🔔", f"明日開盤賣單 {len(_xs)} 檔", _body),
                                unsafe_allow_html=True)
                else:
                    st.markdown(_card("dt-ok", "🔔", "出場檢查", "全部續抱，無賣單"),
                                unsafe_allow_html=True)
            _rf, _pf = _stage_row("fills")
            if _rf is not None and (_pf.get("entries") or _pf.get("exits")):
                _lines = [f"賣出 {_esc(e2.get('stock_id'))} @ {float(e2.get('exit_price') or 0):.2f}"
                          f"（淨 {float(e2.get('net_return_pct') or 0):+.1f}%）"
                          for e2 in (_pf.get("exits") or [])]
                _lines += [f"買進 {_esc(e2.get('stock_id'))} @ {float(e2.get('entry_price') or 0):.2f}"
                           for e2 in (_pf.get("entries") or [])]
                st.markdown(_card("dt-ok", "✅", "今日開盤已成交（昨日掛單）", "<br>".join(_lines)),
                            unsafe_allow_html=True)
            st.markdown(_status_card("risk_gate", "🛡️", "風控閘門"), unsafe_allow_html=True)

    # ═══ 工程視圖：逐段耗時 + 原始紀錄（回答「20 分鐘花在哪」）═══
    with tab_eng:
        _tdf = _stages[["stage", "duration_ms"]].copy()
        _tdf["秒"] = (_tdf["duration_ms"] / 1000).round(1)
        _tdf["階段"] = _tdf["stage"].map(lambda x: _STAGE_LABEL.get(x, x))
        st.bar_chart(_tdf.set_index("階段")["秒"], horizontal=True)

        st.markdown("---")
        for _, _row in _stages.iterrows():
            _icon = "❌" if _row["status"] == "failed" else "✅"
            _dur = f"{(_row['duration_ms'] or 0)/1000:.1f}s"
            _llm = f"｜LLM×{_i(_row['model_calls'])}" if _i(_row["model_calls"]) else ""
            _title = f"{_icon} {_STAGE_LABEL.get(_row['stage'], _row['stage'])}（{_dur}{_llm}）"
            with st.expander(_title, expanded=False):
                if _row["summary"]:
                    st.markdown(f"**{_row['summary']}**")
                if _row["error_msg"]:
                    st.error(_row["error_msg"])
                if _row["sources"]:
                    _src = _row["sources"] if isinstance(_row["sources"], list) else json.loads(_row["sources"])
                    st.caption("資料來源：" + "、".join(
                        f"{x.get('source', '?')}（信心 {x.get('confidence', '—')}）" for x in _src))
                _pl = _row["payload"]
                if _pl is not None:
                    if isinstance(_pl, str):
                        _pl = json.loads(_pl)
                    # 辯論/裁決全文用文字呈現，結構化資料用 json/table
                    if isinstance(_pl, dict) and "text" in _pl and len(_pl) == 1:
                        st.text(_pl["text"])
                    elif isinstance(_pl, dict) and "top_candidates" in _pl:
                        st.caption(f"熱門族群：{'、'.join(_pl.get('hot_sectors') or [])}")
                        st.dataframe(pd.DataFrame(_pl["top_candidates"]),
                                     use_container_width=True, hide_index=True)
                    else:
                        st.json(_pl, expanded=False)


# ══════════════════════════════════════════════════════════════════
#  Page 10：帳號管理（僅管理員）
# ══════════════════════════════════════════════════════════════════
elif page == "👤 帳號管理":
    st.title("👤 帳號管理")
    if USER["role"] != "admin":
        st.error("只有管理員能使用此頁")
        st.stop()

    from database.users import create_user, list_users, set_password, set_telegram_chat

    # 使用者清單
    users = list_users()
    st.subheader(f"使用者（{len(users)} 位）")
    st.dataframe(pd.DataFrame([{
        "ID": u["user_id"], "帳號": u["username"], "顯示名稱": u["display_name"],
        "角色": u["role"], "Telegram Chat ID": u["telegram_chat_id"] or "—",
        "建立時間": str(u["created_at"])[:16],
    } for u in users]), use_container_width=True, hide_index=True)

    st.markdown("---")
    c_new, c_manage = st.columns(2)

    # 新增使用者
    with c_new:
        st.subheader("➕ 新增使用者")
        with st.form("create_user", clear_on_submit=True):
            nu = st.text_input("帳號（英數）")
            np_ = st.text_input("密碼", type="password")
            nd = st.text_input("顯示名稱（選填）")
            nr = st.selectbox("角色", ["user", "admin"])
            ntg = st.text_input("Telegram Chat ID（選填，綁定後 Bot 指令歸戶）")
            ok_new = st.form_submit_button("建立")
        if ok_new:
            ok, msg = create_user(nu, np_, nd or None, nr, ntg or None)
            (st.success if ok else st.error)(msg)

    # 重設密碼 / 綁 Telegram
    with c_manage:
        st.subheader("🔧 管理既有使用者")
        target = st.selectbox("選擇使用者", [u["username"] for u in users])
        with st.form("reset_pw", clear_on_submit=True):
            pw2 = st.text_input("新密碼", type="password")
            ok_pw = st.form_submit_button("重設密碼")
        if ok_pw and pw2:
            st.success("密碼已更新") if set_password(target, pw2) else st.error("更新失敗")
        with st.form("bind_tg", clear_on_submit=True):
            tg2 = st.text_input("Telegram Chat ID（空白=解除綁定）")
            ok_tg = st.form_submit_button("更新綁定")
        if ok_tg:
            st.success("已更新") if set_telegram_chat(target, tg2) else st.error("更新失敗")

    st.caption("提醒：策略參數（strategy.py）與 AI 推薦是全系統共用的；"
               "使用者間隔離的是「我的持倉」「追蹤清單」。")
