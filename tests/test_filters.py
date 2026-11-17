import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import RateLimiter, is_allowed, load_config  # noqa: E402
from reply import channel_permitted, split_text  # noqa: E402

ENV = {
    "DISCORD_TOKEN": "placeholder",
    "GROK_WEBHOOK_URL": "https://example.invalid/hook",
    "GROK_WEBHOOK_KEY": "placeholder",
    "ALLOWED_GUILD_IDS": "1",
    "ALLOWED_CHANNEL_IDS": "10, 11",
    "ALLOWED_USER_IDS": "100",
}


@pytest.mark.parametrize("key", ["ALLOWED_GUILD_IDS", "ALLOWED_CHANNEL_IDS", "ALLOWED_USER_IDS"])
def test_empty_whitelist_fails_closed(key: str) -> None:
    with pytest.raises(SystemExit):
        load_config({**ENV, key: ""})


def test_non_https_webhook_rejected() -> None:
    with pytest.raises(SystemExit):
        load_config({**ENV, "GROK_WEBHOOK_URL": "http://example.invalid"})


def test_config_repr_hides_secrets() -> None:
    secrets = {"DISCORD_TOKEN": "tok-s3cret", "GROK_WEBHOOK_URL": "https://hook.invalid/u-s3cret", "GROK_WEBHOOK_KEY": "key-s3cret"}
    assert "s3cret" not in repr(load_config({**ENV, **secrets}))


def test_is_allowed() -> None:
    cfg = load_config(ENV)
    assert is_allowed(cfg, 1, 10, 100)
    assert is_allowed(cfg, 1, 11, 100)
    assert not is_allowed(cfg, 2, 10, 100)  # 伺服器不符
    assert not is_allowed(cfg, 1, 12, 100)  # 頻道不符
    assert not is_allowed(cfg, 1, 10, 101)  # 使用者不符
    assert not is_allowed(cfg, None, 10, 100)  # 私訊


def test_rate_limiter() -> None:
    rl = RateLimiter(limit=3, window=60)
    assert all(rl.allow(1, now=t) for t in (0, 1, 2))
    assert not rl.allow(1, now=30)
    assert rl.allow(2, now=30)  # 不同使用者各自計算
    assert rl.allow(1, now=60)  # 第一筆過期
    assert not rl.allow(1, now=60.5)


def test_split_text_prefers_newline() -> None:
    text = "a" * 1500 + "\n" + "b" * 1000
    assert split_text(text) == ["a" * 1500, "b" * 1000]


def test_split_text_hard_cut() -> None:
    chunks = split_text("x" * 4500)
    assert [len(c) for c in chunks] == [2000, 2000, 500]
    assert "".join(chunks) == "x" * 4500
    assert split_text("") == []  # 空字串不能變成 [""]，否則會送出空訊息


def test_split_text_all_within_limit() -> None:
    text = (("字" * 37 + " ") * 200).strip()  # reply.py 先 strip 才切
    chunks = split_text(text)
    assert all(0 < len(c) <= 2000 for c in chunks)
    assert len(chunks) > 1 and " ".join(chunks) == text  # 在空白處切、不掉字


def test_channel_permitted() -> None:
    assert not channel_permitted("10", None, None)  # 都沒設 → fail closed
    assert channel_permitted("10", "10,11", None)
    assert channel_permitted("10", None, "10")
    assert not channel_permitted("12", "10,11", None)
    assert not channel_permitted("10", None, "11")
    assert not channel_permitted("12", "10", "12")  # 旗標不能放寬環境變數
    assert not channel_permitted("10", "", "")


def test_history_drops_non_whitelisted() -> None:
    import asyncio
    from datetime import datetime
    from types import SimpleNamespace as NS

    from bridge import Bridge

    def msg(author: int) -> NS:
        return NS(author=NS(id=author, display_name="x"), content="c", created_at=datetime(2026, 1, 1))

    async def history(**_: object):  # type: ignore[no-untyped-def]
        for m in (msg(42), msg(666), msg(100)):  # 新到舊：bot、陌生人、白名單
            yield m

    message = NS(guild=NS(id=1), channel=NS(id=10, history=history), id=5,
                 author=NS(id=100, display_name="x"), content="hi", attachments=[])
    payload = asyncio.run(Bridge._build_payload(NS(cfg=load_config(ENV)), message, 42))  # type: ignore[arg-type]
    assert [m["author_id"] for m in payload["recent_messages"]] == ["100", "42"]
