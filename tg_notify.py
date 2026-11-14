#!/usr/bin/env python3
"""Copy a duty question into a Telegram chat, so the owner sees it on the phone too.

Optional. Needs state/telegram.json next to codex_wake.py, e.g.
  {"python": "~/path/to/venv-with-telethon/bin/python", "tg": "~/.claude/tools/tg/tg.py", "chat": "notes"}
"tg" is a module with client() (a Telethon client) and chat_id(slug). No config -> nothing happens.
Usage (codex_wake.py calls it): tg_notify.py <text>
"""
import json
import subprocess
import sys
from pathlib import Path

CONF = Path(__file__).resolve().parent / 'state' / 'telegram.json'

SENDER = r'''
import asyncio, importlib.util, sys
spec = importlib.util.spec_from_file_location("tg", sys.argv[1]); tg = importlib.util.module_from_spec(spec); spec.loader.exec_module(tg)
async def main():
    cl = tg.client()
    await cl.connect()
    if not await cl.is_user_authorized():
        sys.exit("telegram session is not logged in")
    await cl.send_message(tg.chat_id(sys.argv[2]), sys.argv[3], link_preview=False)
    await cl.disconnect()
asyncio.run(main())
'''


def send(text):
    if not CONF.exists():
        return False, 'no state/telegram.json'
    c = json.loads(CONF.read_text())
    py, tg = Path(c['python']).expanduser(), Path(c['tg']).expanduser()
    # telegram caps a message at 4096 chars
    r = subprocess.run([str(py), '-I', '-c', SENDER, str(tg), c['chat'], text[:4000]],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-300:]


if __name__ == '__main__':
    ok, err = send(' '.join(sys.argv[1:]) or sys.stdin.read())
    print('sent' if ok else f'not sent: {err}')
    sys.exit(0 if ok else 1)
