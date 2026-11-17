"""Discord ↔ Grok Bot 橋接器：收到白名單內的 @提及 後，轉送到 Grok webhook 叫醒 bot。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Mapping

import aiohttp
import discord
from dotenv import load_dotenv

log = logging.getLogger("bridge")

EYES, WARN, HOURGLASS = "👀", "⚠️", "⏳"
HISTORY_LIMIT = 8
RATE_LIMIT, RATE_WINDOW = 3, 60.0
RETRIES = 3
WEBHOOK_TIMEOUT = aiohttp.ClientTimeout(total=15)
FAIL_TEXT = "暫時叫不醒 bot，請稍後再試"
WHITELIST_KEYS = ("ALLOWED_GUILD_IDS", "ALLOWED_CHANNEL_IDS", "ALLOWED_USER_IDS")


def parse_ids(raw: str | None) -> frozenset[int]:
    """逗號分隔的 ID 字串 → 整數集合；格式錯誤直接拋 ValueError。"""
    return frozenset(int(x) for x in (raw or "").split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    # 機密欄位 repr=False，避免被 log 或 traceback 印出
    token: str = field(repr=False)
    webhook_url: str = field(repr=False)
    webhook_key: str = field(repr=False)
    guilds: frozenset[int]
    channels: frozenset[int]
    users: frozenset[int]


def load_config(env: Mapping[str, str]) -> Config:
    """讀設定；缺值或任何白名單為空就拒絕啟動（fail closed）。"""
    missing = [k for k in ("DISCORD_TOKEN", "GROK_WEBHOOK_URL", "GROK_WEBHOOK_KEY") if not env.get(k)]
    if missing:
        raise SystemExit(f"缺少環境變數：{', '.join(missing)}")
    if not env["GROK_WEBHOOK_URL"].startswith("https://"):
        raise SystemExit("GROK_WEBHOOK_URL 必須是 https://")
    lists = {k: parse_ids(env.get(k)) for k in WHITELIST_KEYS}
    empty = [k for k, v in lists.items() if not v]
    if empty:
        raise SystemExit(f"白名單不可為空，拒絕執行：{', '.join(empty)}")
    return Config(
        env["DISCORD_TOKEN"], env["GROK_WEBHOOK_URL"], env["GROK_WEBHOOK_KEY"],
        lists["ALLOWED_GUILD_IDS"], lists["ALLOWED_CHANNEL_IDS"], lists["ALLOWED_USER_IDS"],
    )


def is_allowed(cfg: Config, guild_id: int | None, channel_id: int, author_id: int) -> bool:
    return guild_id in cfg.guilds and channel_id in cfg.channels and author_id in cfg.users


def strip_mentions(content: str, bot_id: int) -> str:
    return re.sub(rf"<@!?{bot_id}>", "", content).strip()


class RateLimiter:
    """滑動視窗：每個 key 在 window 秒內最多 limit 次。"""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW) -> None:
        self.limit, self.window = limit, window
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, key: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


class Bridge(discord.Client):
    def __init__(self, cfg: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        # 橋接器自己發的訊息不 ping 任何人
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.cfg = cfg
        self.limiter = RateLimiter()
        self.webhook_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.webhook_session = aiohttp.ClientSession(timeout=WEBHOOK_TIMEOUT)

    async def close(self) -> None:
        if self.webhook_session:
            await self.webhook_session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("已登入為 %s (id=%s)", self.user, self.user.id if self.user else "?")

    async def on_message(self, message: discord.Message) -> None:
        me = self.user
        if me is None or message.author.bot or message.guild is None:
            return
        if not is_allowed(self.cfg, message.guild.id, message.channel.id, message.author.id):
            return
        if not await self._addressed_to_me(message, me.id):
            return
        if not self.limiter.allow(message.author.id):
            log.info("限流 user=%s message=%s", message.author.id, message.id)
            await self._react(message, HOURGLASS)
            return

        await self._react(message, EYES)
        try:
            await self._post_with_retry(await self._build_payload(message, me.id))
        except Exception:
            log.exception("轉送失敗 message=%s", message.id)
            await self._unreact(message, EYES)
            await self._react(message, WARN)
            try:
                await message.channel.send(FAIL_TEXT)
            except discord.HTTPException:
                log.warning("無法發送失敗通知 channel=%s", message.channel.id)

    async def _addressed_to_me(self, message: discord.Message, bot_id: int) -> bool:
        if any(u.id == bot_id for u in message.mentions):
            return True
        ref = message.reference
        if ref is None or ref.message_id is None:
            return False
        resolved = ref.resolved
        if resolved is None:
            try:
                resolved = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return False
        return isinstance(resolved, discord.Message) and resolved.author.id == bot_id

    async def _build_payload(self, message: discord.Message, bot_id: int) -> dict[str, Any]:
        # 只帶白名單使用者和 bot 自己的發言，非白名單的人碰不到 Grok（防 prompt injection）
        # ponytail: 先抓 8 則再過濾，實際可能少於 8 則；要固定則數再改成多抓
        history = [
            m async for m in message.channel.history(limit=HISTORY_LIMIT, before=message)
            if m.author.id in self.cfg.users or m.author.id == bot_id
        ]
        history.reverse()  # 舊到新
        return {
            "source": "discord",
            "guild_id": str(message.guild.id) if message.guild else None,
            "channel_id": str(message.channel.id),
            "message_id": str(message.id),
            "author_id": str(message.author.id),
            "author_name": message.author.display_name,
            "content": strip_mentions(message.content, bot_id),
            "attachments": [a.url for a in message.attachments],
            "recent_messages": [
                {
                    "author_id": str(m.author.id),
                    "author_name": m.author.display_name,
                    "content": m.content,
                    "created_at": m.created_at.isoformat(),
                }
                for m in history
            ],
            "reply_hint": (
                f"Reply with Discord REST API: POST /channels/{message.channel.id}/messages "
                f"with message_reference to {message.id}"
            ),
        }

    async def _post_with_retry(self, payload: dict[str, Any]) -> None:
        assert self.webhook_session is not None
        headers = {"Authorization": f"Bearer {self.cfg.webhook_key}"}
        for attempt in range(RETRIES + 1):
            try:
                async with self.webhook_session.post(self.cfg.webhook_url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        log.info("已叫醒 Grok message=%s", payload["message_id"])
                        return
                    log.warning("webhook 回應 HTTP %s（第 %d 次）", resp.status, attempt + 1)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # 只記錯誤類型，避免例外訊息帶出 URL
                log.warning("webhook 錯誤 %s（第 %d 次）", type(e).__name__, attempt + 1)
            if attempt < RETRIES:
                await asyncio.sleep(2**attempt)  # 1, 2, 4 秒
        raise RuntimeError(f"webhook 重試 {RETRIES} 次仍失敗")

    async def _react(self, message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            log.warning("無法加 reaction %s message=%s", emoji, message.id)

    async def _unreact(self, message: discord.Message, emoji: str) -> None:
        try:
            await message.remove_reaction(emoji, self.user)
        except discord.HTTPException:
            log.warning("無法移除 reaction %s message=%s", emoji, message.id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    cfg = load_config(os.environ)
    Bridge(cfg).run(cfg.token, log_handler=None)


if __name__ == "__main__":
    main()
