import sys, os, json
sys.path.insert(0, '.')
from config.settings import APIConfig
import requests

token = APIConfig.TELEGRAM_TOKEN
chat_id = APIConfig.TELEGRAM_CHAT_ID
print(f"Token set: {bool(token)}")
print(f"ChatID: {chat_id}")

if not token:
    print("ERROR: no token")
    sys.exit(1)

resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"limit": 10}, timeout=10)
data = resp.json()
print(f"getUpdates ok: {data['ok']}")
updates = data.get("result", [])
print(f"Pending updates: {len(updates)}")
for u in updates:
    msg = u.get("message", {})
    from_chat = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    print(f"  update_id={u['update_id']} from_chat={from_chat} text={repr(text)}")

state_file = ".telegram_state.json"
if os.path.exists(state_file):
    with open(state_file) as f:
        print(f"Stored offset: {json.load(f)}")
else:
    print("No state file (offset=0, all history visible)")
