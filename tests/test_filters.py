import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import RateLimiter, is_allowed, load_config, parse_ids, strip_mentions  # noqa: E402
from reply import channel_permitted, split_text  # noqa: E402

ENV = {
    "DISCORD_TOKEN": "placeholder",
    "GROK_WEBHOOK_URL": "https://example.invalid/hook",
    "GROK_WEBHOOK_KEY": "placeholder",
    "ALLOWED_GUILD_IDS": "1",
    "ALLOWED_CHANNEL_IDS": "10, 11",
    "ALLOWED_USER_IDS": "100",
}


def test_parse_ids() -> None:
    assert parse_ids(" 1, 2,,3 ") == {1, 2, 3}
    assert parse_ids(None) == frozenset()
    with pytest.raises(ValueError):
        parse_ids("abc")


@pytest.mark.parametrize("key", ["ALLOWED_GUILD_IDS", "ALLOWED_CHANNEL_IDS", "ALLOWED_USER_IDS"])
def test_empty_whitelist_fails_closed(key: str) -> None:
    with pytest.raises(SystemExit):
        load_config({**ENV, key: ""})


def test_non_https_webhook_rejected() -> None:
    with pytest.raises(SystemExit):
        load_config({**ENV, "GROK_WEBHOOK_URL": "http://example.invalid"})


def test_config_repr_hides_secrets() -> None:
    assert "placeholder" not in repr(load_config(ENV))


def test_is_allowed() -> None:
    cfg = load_config(ENV)
    assert is_allowed(cfg, 1, 10, 100)
    assert is_allowed(cfg, 1, 11, 100)
    assert not is_allowed(cfg, 2, 10, 100)  # 伺服器不符
    assert not is_allowed(cfg, 1, 12, 100)  # 頻道不符
    assert not is_allowed(cfg, 1, 10, 101)  # 使用者不符
    assert not is_allowed(cfg, None, 10, 100)  # 私訊


def test_strip_mentions() -> None:
    assert strip_mentions("<@42> 你好", 42) == "你好"
    assert strip_mentions("嗨 <@!42> 在嗎 <@42>", 42) == "嗨  在嗎"
    assert strip_mentions("<@7> 叫別人", 42) == "<@7> 叫別人"


def test_rate_limiter() -> None:
    rl = RateLimiter(limit=3, window=60)
    assert all(rl.allow(1, now=t) for t in (0, 1, 2))
    assert not rl.allow(1, now=30)
    assert rl.allow(2, now=30)  # 不同使用者各自計算
    assert rl.allow(1, now=60)  # 第一筆過期
    assert not rl.allow(1, now=60.5)


def test_split_text_short() -> None:
    assert split_text("hello") == ["hello"]
    assert split_text("") == []


def test_split_text_prefers_newline() -> None:
    text = "a" * 1500 + "\n" + "b" * 1000
    assert split_text(text) == ["a" * 1500, "b" * 1000]


def test_split_text_hard_cut() -> None:
    chunks = split_text("x" * 4500)
    assert [len(c) for c in chunks] == [2000, 2000, 500]
    assert "".join(chunks) == "x" * 4500


def test_split_text_all_within_limit() -> None:
    text = ("字" * 37 + " ") * 200
    chunks = split_text(text)
    assert all(0 < len(c) <= 2000 for c in chunks)


def test_channel_permitted() -> None:
    assert channel_permitted("10", None, None)
    assert channel_permitted("10", "10,11", None)
    assert not channel_permitted("12", "10,11", None)
    assert not channel_permitted("10", None, "11")
    assert not channel_permitted("12", "10", "12")  # 旗標不能放寬環境變數
    assert channel_permitted("10", "", "")
