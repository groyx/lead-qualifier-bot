import json

import pytest

from leadbot import (
    COLD,
    HOT,
    LeadBot,
    MockCRM,
    MockTransport,
    RateLimiter,
    RetryingCRM,
    SessionStore,
    score,
)

FULL_RUN = ("Квартира", "9 млн", "1-3 месяца", "Ипотека одобрена", "89991234567")


async def noop_sleep(_):
    return None


def build(store=None, crm=None):
    transport = MockTransport()
    crm = crm if crm is not None else MockCRM()
    bot = LeadBot(transport, crm, store=store if store is not None else SessionStore())
    return bot, transport, crm


@pytest.mark.asyncio
async def test_happy_path_reaches_crm():
    bot, transport, crm = build()

    await bot.handle(1, "/start")
    for answer in FULL_RUN:
        await bot.handle(1, answer)

    assert len(crm.created) == 1
    lead = crm.created[0]
    assert lead.phone == "+79991234567"
    assert lead.score.temperature == HOT
    assert "менеджер свяжется" in transport.texts()[-1].casefold()


@pytest.mark.asyncio
async def test_first_message_starts_the_form_even_without_start_command():
    bot, transport, _ = build()

    await bot.handle(5, "здравствуйте")

    assert "Что рассматриваете?" in transport.last.text
    assert transport.last.options[0] == "Квартира"


@pytest.mark.asyncio
async def test_restart_clears_previous_answers():
    bot, transport, _ = build()
    await bot.handle(1, "/start")
    await bot.handle(1, "Квартира")

    await bot.handle(1, "/start")

    assert bot.store.get(1).answers == {}
    assert bot.store.get(1).step == 0


@pytest.mark.asyncio
async def test_second_completion_does_not_create_a_duplicate_lead():
    bot, transport, crm = build()
    await bot.handle(1, "/start")
    for answer in FULL_RUN:
        await bot.handle(1, answer)

    await bot.handle(1, "ещё раз")

    assert len(crm.created) == 1
    assert "уже у менеджера" in transport.texts()[-1]


@pytest.mark.asyncio
async def test_crm_outage_does_not_lose_the_lead():
    crm = MockCRM(fail_times=99)
    bot, transport, _ = build(crm=RetryingCRM(crm, attempts=2, sleep=noop_sleep))

    await bot.handle(1, "/start")
    for answer in FULL_RUN:
        await bot.handle(1, answer)

    # пользователю не показали стектрейс, а ответы остались в сессии
    assert "заминка" in transport.texts()[-1]
    session = bot.store.get(1)
    assert session.finished
    assert session.answers["phone"] == "+79991234567"
    assert session.crm_id is None


@pytest.mark.asyncio
async def test_two_chats_do_not_mix_answers():
    bot, _, crm = build()

    await bot.handle(1, "/start")
    await bot.handle(2, "/start")
    await bot.handle(1, "Квартира")
    await bot.handle(2, "Участок")

    assert bot.store.get(1).answers["property_type"] == "Квартира"
    assert bot.store.get(2).answers["property_type"] == "Участок"


@pytest.mark.asyncio
async def test_unfinished_form_survives_a_restart(tmp_path):
    path = tmp_path / "sessions.json"

    bot, _, _ = build(store=SessionStore(path))
    await bot.handle(1, "/start")
    await bot.handle(1, "Квартира")
    await bot.handle(1, "9 млн")

    # процесс перезапустился: новый стор из того же файла
    revived, transport, crm = build(store=SessionStore(path))
    for answer in ("1-3 месяца", "Ипотека одобрена", "89991234567"):
        await revived.handle(1, answer)

    assert len(crm.created) == 1, "лид должен дойти, а не начаться заново"
    assert crm.created[0].answers["budget"] == 9_000_000


@pytest.mark.asyncio
async def test_broken_state_file_does_not_block_startup(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{ это не json", encoding="utf-8")

    bot, transport, _ = build(store=SessionStore(path))
    await bot.handle(1, "привет")

    assert "Что рассматриваете?" in transport.last.text


def test_flush_is_atomic(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.start(1)
    store.flush()

    class Exploding(dict):
        def values(self):
            raise RuntimeError("диск умер посреди записи")

    store._sessions = Exploding()
    with pytest.raises(RuntimeError):
        store.flush()

    assert json.loads(path.read_text(encoding="utf-8"))["sessions"][0]["chat_id"] == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_cold_lead_is_scored_low():
    answers = {
        "property_type": "Участок",
        "budget": 1_500_000,
        "timeline": "Пока просто смотрю",
        "financing": "Ещё не решил",
    }
    result = score(answers)
    assert result.temperature == COLD
    assert result.points == 0


@pytest.mark.asyncio
async def test_rate_limiter_spaces_out_messages_to_one_chat():
    clock = [0.0]
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    limiter = RateLimiter(now=lambda: clock[0], sleep=fake_sleep)

    await limiter.acquire(1)
    await limiter.acquire(1)

    assert slept == [pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_rate_limiter_does_not_delay_different_chats():
    clock = [0.0]
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    limiter = RateLimiter(now=lambda: clock[0], sleep=fake_sleep)

    await limiter.acquire(1)
    await limiter.acquire(2)

    assert slept == []


@pytest.mark.asyncio
async def test_rate_limiter_enforces_the_global_ceiling():
    clock = [0.0]
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    limiter = RateLimiter(global_limit=3, now=lambda: clock[0], sleep=fake_sleep)

    for chat_id in range(4):
        await limiter.acquire(chat_id)

    assert len(slept) == 1, "четвёртое сообщение за секунду должно подождать"
