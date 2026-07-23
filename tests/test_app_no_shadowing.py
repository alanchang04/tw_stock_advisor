"""
app.py 變數覆蓋靜態檢查（2026-07-22 新增，實際線上炸過才補的）。

事件：個股分析頁的股票背景卡裡，把相對強度的強弱標籤命名成 `_st`，撞到外層
`_st = _result["stages"]`（給 _sa_stage()/_sa_meta() 兩個閉包查各段落用的字典），
把字典蓋成字串「極弱」，導致後面 `_sa_stage("market_regime")` 內的 `_st.get(name)`
直接 AttributeError，整個個股分析頁在 Streamlit Cloud 上炸掉。

單元測試抓不到這種錯（那是 Streamlit 腳本層的變數作用域問題，不是函式邏輯），
先前的「啟動冒煙測試」也只確認 app 起得來、沒真的渲染頁面，所以漏掉。
這裡用 AST 檢查：app.py 裡這些「基礎設施變數」全檔只能被指派一次，
任何地方不小心拿同名變數當暫存值都會被擋下來。
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")

# 這些名字被閉包/後續段落依賴，重複指派＝覆蓋風險
PROTECTED_NAMES = ["_st", "_result"]


def _assignment_lines(tree, name):
    """回傳所有把 name 當賦值目標（含 tuple unpack / for 迴圈變數）的行號。"""
    lines = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            targets = [it.optional_vars for it in node.items if it.optional_vars]
        for t in targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name) and sub.id == name:
                    lines.append(node.lineno)
    return sorted(set(lines))


def test_protected_names_assigned_only_once():
    with open(APP_PATH, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for name in PROTECTED_NAMES:
        lines = _assignment_lines(tree, name)
        assert len(lines) <= 1, (
            f"app.py 的 `{name}` 被指派 {len(lines)} 次（行 {lines}）——"
            f"這個名字被 _sa_stage()/_sa_meta() 等閉包依賴，重複指派會把它蓋掉，"
            f"造成執行期 AttributeError。請把暫存變數改名。"
        )


def test_app_py_parses():
    """順帶確保 app.py 語法正確（改壞語法時測試就會紅，不用等 Streamlit 起不來）。"""
    with open(APP_PATH, encoding="utf-8") as f:
        ast.parse(f.read())
