import pytest

from leadbot import (
    Bitrix24CRM,
    CRMRejected,
    CRMUnavailable,
    Lead,
    MockCRM,
    RetryingCRM,
    score,
)


def make_lead(chat_id=1, phone="+79991234567", budget=9_000_000):
    answers = {
        "property_type": "Квартира",
        "budget": budget,
        "timeline": "1-3 месяца",
        "financing": "Ипотека одобрена",
        "phone": phone,
    }
    return Lead(chat_id=chat_id, phone=phone, answers=answers, score=score(answers))


async def noop_sleep(_):
    return None


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_failures():
    crm = MockCRM(fail_times=2)
    client = RetryingCRM(crm, attempts=3, sleep=noop_sleep)

    crm_id = await client.create_lead(make_lead())

    assert crm_id == "mock-1"
    assert crm.attempts == 3


@pytest.mark.asyncio
async def test_retry_gives_up_and_reports():
    crm = MockCRM(fail_times=99)
    client = RetryingCRM(crm, attempts=3, sleep=noop_sleep)

    with pytest.raises(CRMUnavailable, match="после 3 попыток"):
        await client.create_lead(make_lead())

    assert crm.attempts == 3


@pytest.mark.asyncio
async def test_rejected_lead_is_not_retried():
    """Битые данные повтором не чинятся, незачем жечь попытки."""
    crm = MockCRM(fail_times=99, error=CRMRejected)
    client = RetryingCRM(crm, attempts=5, sleep=noop_sleep)

    with pytest.raises(CRMRejected):
        await client.create_lead(make_lead())

    assert crm.attempts == 1


@pytest.mark.asyncio
async def test_duplicate_submission_creates_one_lead():
    crm = MockCRM()
    client = RetryingCRM(crm, sleep=noop_sleep)
    lead = make_lead()

    first = await client.create_lead(lead)
    second = await client.create_lead(lead)

    assert first == second
    assert len(crm.created) == 1
    assert crm.attempts == 1, "второй раз в CRM ходить не должны"


@pytest.mark.asyncio
async def test_different_people_are_not_deduplicated():
    crm = MockCRM()
    client = RetryingCRM(crm, sleep=noop_sleep)

    await client.create_lead(make_lead(chat_id=1, phone="+79991234567"))
    await client.create_lead(make_lead(chat_id=2, phone="+79997654321"))

    assert len(crm.created) == 2


@pytest.mark.asyncio
async def test_bitrix_sends_expected_fields():
    captured = {}

    async def fake_http(url, fields):
        captured["url"] = url
        captured["fields"] = fields
        return {"result": 777}

    crm_id = await Bitrix24CRM("https://co.bitrix24.ru/rest/1/tok", fake_http).create_lead(
        make_lead()
    )

    assert crm_id == "777"
    assert captured["url"].endswith("/crm.lead.add.json")
    assert captured["fields"]["fields[PHONE][0][VALUE]"] == "+79991234567"
    assert captured["fields"]["fields[OPPORTUNITY]"] == 9_000_000
    assert "HOT" in captured["fields"]["fields[TITLE]"]


@pytest.mark.asyncio
async def test_bitrix_error_payload_becomes_rejected():
    async def fake_http(url, fields):
        return {"error": "INVALID_FIELD", "error_description": "поле не заполнено"}

    with pytest.raises(CRMRejected, match="поле не заполнено"):
        await Bitrix24CRM("https://x/rest", fake_http).create_lead(make_lead())


@pytest.mark.asyncio
async def test_network_failure_becomes_unavailable_not_a_crash():
    async def fake_http(url, fields):
        raise TimeoutError("сеть отвалилась")

    with pytest.raises(CRMUnavailable):
        await Bitrix24CRM("https://x/rest", fake_http).create_lead(make_lead())


def test_comment_includes_scoring_reasons():
    comment = make_lead().comment()
    assert "9 000 000 ₽" in comment
    assert "ипотека уже одобрена" in comment
