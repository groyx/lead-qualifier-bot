import pytest

from leadbot import Dialogue, Session, ValidationError, parse_budget, parse_phone


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5 млн", 5_000_000),
        ("5млн", 5_000_000),
        ("5000000", 5_000_000),
        ("5 000 000", 5_000_000),
        ("5,5 млн", 5_500_000),
        ("5.5 млн", 5_500_000),
        ("800к", 800_000),
        ("800 тыс", 800_000),
        ("примерно 12 миллионов", 12_000_000),
        ("от 4 до 6 млн", 4_000_000),  # берём нижнюю границу
        ("\xa07\xa0млн", 7_000_000),  # неразрывные пробелы из объявлений
    ],
)
def test_parse_budget(raw, expected):
    assert parse_budget(raw) == expected


@pytest.mark.parametrize("raw", ["дорого", "", "50", "999999999999 млрд"])
def test_parse_budget_rejects_garbage(raw):
    with pytest.raises(ValidationError):
        parse_budget(raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+7 999 123-45-67", "+79991234567"),
        ("89991234567", "+79991234567"),
        ("8 (999) 123 45 67", "+79991234567"),
        ("9991234567", "+79991234567"),
    ],
)
def test_parse_phone(raw, expected):
    assert parse_phone(raw) == expected


@pytest.mark.parametrize("raw", ["123", "не скажу", "+1 555 0100", "88005553535555"])
def test_parse_phone_rejects_garbage(raw):
    with pytest.raises(ValidationError):
        parse_phone(raw)


def test_option_matching_accepts_number_and_partial():
    d = Dialogue()
    s = Session(chat_id=1)

    d.advance(s, "2")  # второй вариант
    assert s.answers["property_type"] == "Дом"

    s2 = Session(chat_id=2)
    d.advance(s2, "коммерч")  # однозначное частичное совпадение
    assert s2.answers["property_type"] == "Коммерческая"


def test_ambiguous_partial_is_rejected():
    """"Ипотека" подходит и к одобренной, и к нужной: угадывать нельзя."""
    d = Dialogue()
    s = Session(chat_id=1)
    for answer in ("Квартира", "6 млн", "1-3 месяца"):
        d.advance(s, answer)

    step_before = s.step
    reply = d.advance(s, "ипотека")

    assert s.step == step_before, "неоднозначный ответ не должен двигать шаг"
    assert "Выберите один из вариантов" in reply.text


def test_invalid_answer_repeats_the_question_without_advancing():
    d = Dialogue()
    s = Session(chat_id=1)
    d.advance(s, "Квартира")

    reply = d.advance(s, "не знаю")

    assert s.step == 1
    assert "budget" not in s.answers
    assert reply.options == ()


def test_full_run_collects_every_answer():
    d = Dialogue()
    s = Session(chat_id=42)

    for answer in ("Квартира", "9 млн", "1-3 месяца", "Ипотека одобрена", "89991234567"):
        reply = d.advance(s, answer)

    assert reply.completed
    assert s.finished
    assert s.answers == {
        "property_type": "Квартира",
        "budget": 9_000_000,
        "timeline": "1-3 месяца",
        "financing": "Ипотека одобрена",
        "phone": "+79991234567",
    }


def test_answers_after_completion_do_not_reopen_the_form():
    d = Dialogue()
    s = Session(chat_id=1, finished=True)

    reply = d.advance(s, "ещё что-то")

    assert reply.completed
    assert s.answers == {}


def test_session_survives_a_round_trip():
    s = Session(chat_id=7, step=2, answers={"budget": 5_000_000}, crm_id="abc")
    restored = Session.restore(s.snapshot())
    assert restored == s
