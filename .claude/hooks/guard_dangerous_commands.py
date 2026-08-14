#!/usr/bin/env python
"""PreToolUse 守門：把破壞性指令攔下來要求人工核准。

為什麼需要這個，而不是只靠 settings.json 的 permission rules
--------------------------------------------------------------
permission rules 是**前綴**比對：`Bash(rm -rf *)` 只擋得住開頭就是 `rm -rf`
的指令。但真正會出事的通常長這樣：

    cd /some/path && rm -rf build
    python -c "import shutil; shutil.rmtree('data')"
    git stash push --include-untracked      # 本專案真的發生過

這個 hook 掃**整條指令字串**，所以 `&&`、`;`、管線後面的危險動作一樣抓得到。

三個層級
--------
DENY  : 災難性且不可回復，且在本專案沒有任何正當用途 → 直接拒絕，不給核准選項
ASK   : 有正當用途但會改變狀態或刪東西 → 一律跳出來讓人決定
（其他）: 放行

**這不是沙箱。** 它擋的是「常見寫法」，不是所有可能的寫法。
真正的保護是它 + permission rules + 你自己看一眼。
"""
from __future__ import annotations

import json
import re
import sys

# ── 災難級：直接拒絕，連核准選項都不給 ────────────────────────────────
DENY = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rR][a-zA-Z]*f?\s+(/|~|\$HOME|C:\\\\?\s*$)",
     "rm -rf 指向根目錄或家目錄"),
    (r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r?\s+/\s*$", "rm -rf /"),
    (r"\bmkfs(\.|\s)", "格式化檔案系統"),
    (r"\bdd\s+.*\bof=/dev/", "dd 寫入區塊裝置"),
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bDROP\s+DATABASE\b", "DROP DATABASE"),
    (r"\bgit\s+filter-branch\b", "git filter-branch 會重寫整段歷史"),
    (r"\bgit\s+push\b.*(--force(?!-with-lease)|\s-f\b)",
     "git push --force 會覆寫遠端歷史（--force-with-lease 才會降為詢問）"),
    (r"Remove-Item\b.*-Recurse.*-Force.*\b(C:\\+|\$HOME|~)\s*$",
     "PowerShell 遞迴強制刪除指向磁碟根或家目錄"),
]

# ── 需要人工核准 ────────────────────────────────────────────────────
ASK = [
    # 檔案刪除
    (r"\brm\s+(-\w+\s+)*-\w*[rR]", "遞迴刪除檔案"),
    (r"\brm\s+-\w*f", "強制刪除檔案"),
    (r"Remove-Item\b.*-(Recurse|Force)", "PowerShell 刪除檔案／目錄"),
    (r"\brmdir\s+/s", "Windows 遞迴刪除目錄"),
    (r"\bdel\s+/[sq]", "Windows 批次刪除"),
    (r"shutil\.rmtree|os\.removedirs|Path\([^)]*\)\.unlink|os\.remove",
     "Python 程式碼刪除檔案"),

    # git：會改變歷史或工作區
    (r"\bgit\s+worktree\s+(remove|prune)", "移除 git worktree"),
    (r"\bgit\s+reset\s+.*--hard", "git reset --hard 會丟掉未提交的變更"),
    (r"\bgit\s+clean\b", "git clean 會刪除未追蹤的檔案"),
    (r"\bgit\s+stash\b", "git stash 會把工作區的檔案收走（本專案發生過）"),
    (r"\bgit\s+checkout\s+(--\s|\.\s*$|-- \.)", "git checkout -- 會覆寫工作區"),
    (r"\bgit\s+branch\s+-[dD]\b", "刪除分支"),
    (r"\bgit\s+rebase\b", "rebase 會重寫本地歷史"),
    (r"\bgit\s+reflog\s+expire|\bgit\s+gc\b.*--prune", "清掉可回復的救命繩"),
    (r"\bgit\s+update-ref\s+-d", "刪除 ref"),

    # git：使用者明確要求要經過同意
    (r"\bgit\s+commit\b", "提交（依你的要求一律先問）"),
    (r"\bgit\s+push\b", "推送到遠端（依你的要求一律先問）"),
    (r"\bgit\s+tag\s+-d|\bgit\s+push\b.*--delete", "刪除 tag／遠端分支"),

    # docker
    (r"\bdocker\s+(rm|rmi)\b", "移除 docker 容器／映像"),
    (r"\bdocker\s+.*\bprune\b", "docker prune 會清掉資料"),
    (r"\bdocker\s+volume\s+rm", "刪除 docker volume（資料會消失）"),
    (r"\bdocker[- ]compose\s+down\b.*(-v|--volumes)",
     "compose down -v 會刪掉 volume 裡的資料"),

    # 資料庫
    (r"\bDROP\s+(TABLE|SCHEMA|INDEX)\b", "DROP 資料庫物件"),
    (r"\bTRUNCATE\s+TABLE\b|\bTRUNCATE\s+\w", "TRUNCATE 清空資料表"),
    (r"\bDELETE\s+FROM\b", "DELETE FROM"),
    (r"\bALTER\s+TABLE\b.*\bDROP\b", "ALTER TABLE ... DROP"),

    # 環境
    (r"\bpip\s+(install|uninstall)\b", "改動 Python 環境"),
    (r"\bnpm\s+(install|uninstall|ci)\b", "改動 node 環境"),
    (r"\bschtasks\s+/(create|delete|change)", "改動 Windows 排程"),
    (r">\s*/dev/sd|\bdiskpart\b", "直接操作磁碟"),
]


def decide(command: str) -> tuple[str, str] | None:
    # `git commit` 的訊息本文是任意文字，會把危險樣式當成散文寫進去
    # （本專案第一次啟用這個 hook 時，就被自己的 commit message 擋下來了）。
    # commit 本身不會執行訊息內容，而且它已經是 ASK——人會看到完整指令再決定，
    # 所以這裡直接回 ASK，不對訊息本文跑 DENY 掃描。
    if re.match(r"\s*git\s+commit\b", command, re.IGNORECASE):
        return "ask", "提交（依你的要求一律先問；訊息本文不做樣式掃描）"

    for pattern, reason in DENY:
        if re.search(pattern, command, re.IGNORECASE):
            return "deny", reason
    for pattern, reason in ASK:
        if re.search(pattern, command, re.IGNORECASE):
            return "ask", reason
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)                      # 讀不到就放行，不要把整個工作卡住
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command:
        sys.exit(0)

    verdict = decide(command)
    if verdict is None:
        sys.exit(0)

    decision, reason = verdict
    prefix = "已封鎖" if decision == "deny" else "需要你的同意"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": f"[{prefix}] {reason}",
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
