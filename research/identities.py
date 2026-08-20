"""經濟恆等式檢查（`docs/RESEARCH_METHOD_AMENDMENTS.md` M24）。

為什麼單元測試不夠
------------------
H11b 第一版在建構期間報酬時多做了一次前瞻位移。結果是：

    程式不 crash、形狀正確、日期正確、報酬數值正確、整套測試全綠，
    **但它安靜地答錯了一個月。**

抓到它的不是測試，是一句事前寫下的**算術約束**：

    M+1 的窗完整包含「早窗」，因此 M+1 的效果不可能小於早窗單獨的效果。

實測 M+1 = +0.680%，早窗 = +0.842%。**在算術上不可能**，於是去查實作。

這類約束的力量在於它**不依賴實作**：它來自窗口之間的包含關係本身，
所以實作錯了它就會違反，而實作測試只會檢查實作有沒有做到它自己寫的事。

**規則**：任何把時間窗切開或重新對齊的研究，都必須事前寫下至少一條
這種約束，並在結果出爐時檢查。違反時**先查實作，不要先解釋結果**。
"""
from __future__ import annotations

from dataclasses import dataclass

# 容忍度該用哪個噪音尺度，是這個模組最容易設錯的地方。
#
# **不是**個別係數的 SE（本專案多在 0.10~0.20pp）。包含窗與被包含窗
# **共用**重疊那段資料，因此兩者「差值」的變異只來自**剩餘段**，
# 遠小於任一方單獨的 SE。用個別 SE 去設容忍度會把門檻設得太寬，
# 寬到抓不到真正發生過的錯誤——本專案第一版就是這樣（設 0.30，
# 而實際違反量是 0.162，於是約束靜靜地通過了）。
#
# 剩餘段約 7 個交易日的橫斷面漂移，SE 量級 ~0.08pp，故取其約兩倍。
DEFAULT_TOLERANCE = 0.15

# 加總檢查要寬得多，理由與噪音無關：**兩邊的有效樣本本來就不同**。
# `cumulative_from_path` 需要同一格點的六個領先期都存在，因此會丟掉尾端
# 幾個格點；逐期係數則各自用自己能用的格點。實測這個系統性落差在
# H11（2.555 vs 2.44）與 H11b（3.419 vs 3.251）都是 0.11~0.17pp。
# 它是設計使然而不是錯誤，所以加總檢查只該抓「少了一整段大的」。
ADDITIVITY_TOLERANCE = 0.30


@dataclass(frozen=True)
class IdentityCheck:
    """一次恆等式檢查的結果。``holds=False`` 代表**先去查實作**。"""

    label: str
    holds: bool
    detail: str
    slack: float

    def raise_if_violated(self) -> None:
        if not self.holds:
            raise ValueError(
                f"經濟恆等式被違反：{self.label}。{self.detail} "
                f"這通常代表實作錯誤，不是新發現——先查實作。")


def containment(*, label: str, whole: float, part: float,
                remainder_floor: float = 0.0,
                tolerance: float = DEFAULT_TOLERANCE) -> IdentityCheck:
    """``whole`` 的窗若**完整包含** ``part`` 的窗，則 whole >= part + 剩餘部分。

    ``remainder_floor`` 是「剩下那段最少貢獻多少」的事前下界。預設 0，
    代表只假設剩餘段不是大幅為負——這是很弱的假設，因此違反它幾乎一定
    是實作問題而不是發現。

    這正是抓到 H11b 差一期錯誤的那條約束。**但要誠實說明它的功效**：
    在 ``remainder_floor=0`` 之下，那次違反量（0.162）只略高於容忍度，
    也就是**剛好抓到**。若事前對剩餘段有下界（例如已知它是一段正漂移），
    傳進 ``remainder_floor`` 會讓約束強得多——這是推薦用法。
    """
    required = part + remainder_floor - tolerance
    slack = whole - required
    holds = slack >= 0.0
    return IdentityCheck(
        label=label, holds=holds, slack=slack,
        detail=(f"包含窗 {whole:+.4f} 應 >= 被包含窗 {part:+.4f} "
                f"＋剩餘下界 {remainder_floor:+.4f} −容忍 {tolerance:.2f} "
                f"= {required:+.4f}；差額 {slack:+.4f}。"))


def additivity(*, label: str, whole: float, parts: list[float],
               tolerance: float = ADDITIVITY_TOLERANCE) -> IdentityCheck:
    """把一個窗切成互不重疊且合併後等於整體的數段時，效果應該相加。

    用於檢查「反應路徑加總 = 累積效果」這類關係。報酬嚴格來說是幾何相乘，
    但在本專案的量級（單月數個 pp 以內）線性近似的誤差遠小於 ``tolerance``。

    **這個檢查天生比 ``containment`` 弱**，因為累積與逐期係數的有效樣本
    不同（見 ``ADDITIVITY_TOLERANCE``）。把它當成「有沒有整段掉了」的
    粗篩，不要當成對帳。
    """
    total = sum(parts)
    slack = tolerance - abs(whole - total)
    return IdentityCheck(
        label=label, holds=slack >= 0.0, slack=slack,
        detail=(f"整體 {whole:+.4f} 應 ≈ 各段和 {total:+.4f}"
                f"（差 {whole - total:+.4f}，容忍 {tolerance:.2f}）。"))
