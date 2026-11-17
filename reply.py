#!/usr/bin/env python3
"""給 Grok Bot 用的 Discord 回覆 CLI（只用標準函式庫）。

用法：
  python reply.py --channel <id> [--reply-to <message_id>] --text "..."
  python reply.py --channel <id> [--reply-to <message_id>] --text-file path
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://discord.com/api/v10"
LIMIT = 2000
USER_AGENT = "DiscordBot (https://github.com/grokbot-discord-bridge, 1.0)"


class ApiError(Exception):
    pass


def split_text(text: str, limit: int = LIMIT) -> list[str]:
    """切成每段 ≤ limit 字；優先在換行、其次空白處切，都沒有就硬切。"""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut + 1:] if text[cut] in "\n " else text[cut:]
    if text:
        chunks.append(text)
    return chunks


def _ids(raw: str | None) -> set[str] | None:
    """None/空字串代表「未設定」。"""
    if raw is None or not raw.strip():
        return None
    return {s.strip() for s in raw.split(",") if s.strip()}


def channel_permitted(channel: str, env_raw: str | None, flag_raw: str | None) -> bool:
    """環境變數與 --allowed-channels 有設的都要通過（兩者取交集，旗標無法放寬環境變數）。"""
    return all(allowed is None or channel in allowed for allowed in (_ids(env_raw), _ids(flag_raw)))


def api(method: str, path: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for _ in range(5):
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read()
            if e.code == 429:
                retry_after = float(json.loads(detail or b"{}").get("retry_after", 1))
                time.sleep(min(retry_after, 30))
                continue
            raise ApiError(f"{method} {path} → HTTP {e.code}: {detail[:200]!r}") from None
        except urllib.error.URLError as e:
            raise ApiError(f"{method} {path} → {e.reason}") from None
    raise ApiError(f"{method} {path} → 持續被限流")


def _snowflake(value: str) -> str:
    if not (value.isascii() and value.isdigit()):
        raise argparse.ArgumentTypeError(f"不是合法的 Discord ID：{value!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="以 bot 身分回覆 Discord 頻道")
    p.add_argument("--channel", required=True, type=_snowflake)
    p.add_argument("--reply-to", type=_snowflake)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text")
    src.add_argument("--text-file")
    p.add_argument("--allowed-channels", help="逗號分隔；與環境變數 ALLOWED_CHANNEL_IDS 同時檢查")
    a = p.parse_args(argv)

    if not channel_permitted(a.channel, os.environ.get("ALLOWED_CHANNEL_IDS"), a.allowed_channels):
        print(f"拒絕：頻道 {a.channel} 不在允許名單內", file=sys.stderr)
        return 2
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("缺少環境變數 DISCORD_TOKEN", file=sys.stderr)
        return 2
    if a.text is not None:
        text = a.text
    else:
        with open(a.text_file, encoding="utf-8") as f:
            text = f.read()
    chunks = split_text(text.strip())
    if not chunks:
        print("文字是空的", file=sys.stderr)
        return 2

    try:
        for i, chunk in enumerate(chunks):
            body: dict[str, Any] = {"content": chunk, "allowed_mentions": {"parse": []}}
            if a.reply_to and i == 0:
                body["message_reference"] = {"message_id": a.reply_to, "fail_if_not_exists": False}
            api("POST", f"/channels/{a.channel}/messages", token, body)
    except ApiError as e:
        print(f"送出失敗：{e}", file=sys.stderr)
        return 1

    if a.reply_to:
        base = f"/channels/{a.channel}/messages/{a.reply_to}/reactions"
        for method, emoji in (("DELETE", "👀"), ("PUT", "✅")):
            try:
                api(method, f"{base}/{urllib.parse.quote(emoji)}/@me", token)
            except ApiError as e:
                print(f"reaction 更新失敗（訊息已送出）：{e}", file=sys.stderr)
    print(f"已送出 {len(chunks)} 段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
