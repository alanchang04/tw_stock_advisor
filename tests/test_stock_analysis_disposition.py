"""個股分析對處置股要講出原因，不能只回「不在候選池」。

使用者 2026-07-22 抱怨過「看不出為什麼」——處置是所有排除原因裡最具體的一種，
含混帶過等於浪費了一個能講清楚的機會。
"""
import pandas as pd

from agent.stock_analysis import rank_in_universe


def _universe(hard=None, disposition=None):
    df = pd.DataFrame({"stock_id": ["2330", "2454"], "score": [9.0, 8.0]})
    df.attrs["hard_excluded"] = hard or []
    df.attrs["disposition_excluded"] = disposition or []
    return df


def test_disposition_stock_gets_specific_reason():
    u = _universe(disposition=[{"stock_id": "2434", "stock_name": "統懋"}])
    r = rank_in_universe("2434", u)
    assert r["in_universe"] is False
    assert "處置" in r["veto_reason"]
    assert "預收款券" in r["veto_reason"]      # 講清楚為什麼不能買，不是只說被擋


def test_normal_missing_stock_still_has_no_reason():
    """只是分數不夠/不在池裡的，veto_reason 應維持 None——別把處置理由亂安。"""
    r = rank_in_universe("1101", _universe())
    assert r["in_universe"] is False and r["veto_reason"] is None


def test_hard_veto_still_takes_precedence():
    u = _universe(hard=[{"stock_id": "2434", "hard_veto_reason": "乖離月線15%以上；"}],
                  disposition=[{"stock_id": "2434", "stock_name": "統懋"}])
    assert "乖離" in rank_in_universe("2434", u)["veto_reason"]


def test_in_universe_stock_unaffected():
    r = rank_in_universe("2330", _universe(disposition=[{"stock_id": "2434", "stock_name": "統懋"}]))
    assert r["in_universe"] is True and r["rank"] == 1


def test_missing_attr_does_not_crash():
    """attrs 沒有 disposition_excluded 的舊 universe 物件要照常運作。"""
    df = pd.DataFrame({"stock_id": ["2330"], "score": [9.0]})
    assert rank_in_universe("2330", df)["in_universe"] is True
