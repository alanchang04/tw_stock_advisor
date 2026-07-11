"""
Broker 交易層抽象測試（不需 DB / 不需 shioaji 套件）。
驗證：預設是 PaperBroker、PaperBroker 三個方法正確委派給 portfolio 的階段0b 函式、
      ShioajiBroker 高階流程尚未接（會擋下），且未裝套件時能給清楚錯誤。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.broker as bk
from agent.broker import PaperBroker, ShioajiBroker, get_broker


def test_default_broker_is_paper(monkeypatch):
    monkeypatch.delenv("BROKER", raising=False)
    assert isinstance(get_broker(), PaperBroker)
    assert get_broker().name == "paper"


def test_get_broker_reads_env(monkeypatch):
    monkeypatch.setenv("BROKER", "shioaji")
    assert isinstance(get_broker(), ShioajiBroker)
    monkeypatch.setenv("BROKER", "paper")
    assert isinstance(get_broker(), PaperBroker)


def test_unknown_broker_raises():
    with pytest.raises(ValueError):
        get_broker("nope")


def test_paper_broker_delegates_to_stage0b(monkeypatch):
    """PaperBroker 三個方法必須就是階段0b 的 portfolio 函式，不可各寫一套。"""
    import agent.portfolio as pf
    calls = {}
    def _sync(d):    calls["sync"] = d;    return {"ok": 1}
    def _exits(d):   calls["exits"] = d;   return []
    def _entries(p, d): calls["entries"] = (p, d); return []
    monkeypatch.setattr(pf, "fill_pending_orders", _sync)
    monkeypatch.setattr(pf, "queue_exits", _exits)
    monkeypatch.setattr(pf, "queue_entries", _entries)

    b = PaperBroker()
    assert b.sync("D") == {"ok": 1}
    assert b.submit_exits("D") == []
    assert b.submit_entries([{"stock_id": "2330"}], "D") == []
    assert calls["sync"] == "D"
    assert calls["exits"] == "D"
    assert calls["entries"] == ([{"stock_id": "2330"}], "D")


def test_shioaji_highlevel_not_wired_yet():
    """高階流程尚未接 pipeline，必須明確擋下（避免誤以為已能自動送單）。"""
    b = ShioajiBroker()
    for call in (lambda: b.sync("D"),
                 lambda: b.submit_exits("D"),
                 lambda: b.submit_entries([], "D")):
        with pytest.raises(NotImplementedError):
            call()


def test_shioaji_connect_without_package_gives_clear_error(monkeypatch):
    """未裝 shioaji 時要給人看得懂的錯誤，而不是 ImportError 直接炸。"""
    monkeypatch.setitem(sys.modules, "shioaji", None)  # 強制 import 失敗
    with pytest.raises(RuntimeError, match="shioaji"):
        ShioajiBroker().connect()
