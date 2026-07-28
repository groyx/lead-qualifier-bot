"""Сценарий квалификации лида.

Диалог описан данными, а не кодом: чтобы поменять вопросы, править Python
не нужно. Логика не знает ни про Telegram, ни про CRM, поэтому весь сценарий
целиком проверяется тестами без сети.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable


class ValidationError(Exception):
    """Ответ пользователя не подошёл. Текст показывается пользователю."""


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    options: tuple[str, ...] = ()
    parse: Callable[[str], object] | None = None

    def handle(self, raw: str) -> object:
        answer = raw.strip()
        if not answer:
            raise ValidationError("Пустой ответ, напишите пожалуйста ещё раз.")
        if self.options:
            return _match_option(answer, self.options)
        if self.parse is not None:
            return self.parse(answer)
        return answer


def _match_option(answer: str, options: Iterable[str]) -> str:
    options = tuple(options)
    lowered = answer.casefold()

    for option in options:
        if lowered == option.casefold():
            return option

    # пользователь может прислать номер варианта вместо текста
    if answer.isdigit():
        index = int(answer) - 1
        if 0 <= index < len(options):
            return options[index]

    # частичное совпадение, но только если оно однозначное
    partial = [o for o in options if lowered in o.casefold()]
    if len(partial) == 1:
        return partial[0]

    listing = ", ".join(options)
    raise ValidationError(f"Не понял ответ. Выберите один из вариантов: {listing}")


_MULTIPLIERS = (
    (("млрд", "миллиард"), 1_000_000_000),
    (("млн", "миллион", "лям"), 1_000_000),
    (("тыс", "тысяч", "к"), 1_000),
)


def parse_budget(raw: str) -> int:
    """Понимает "5 млн", "5000000", "5,5 млн", "от 4 до 6 млн", "800к"."""
    text = raw.casefold().replace(" ", " ")
    # склеиваем разряды: "5 000 000" это одно число, а не три.
    # схлопываем пробел только перед группой ровно из трёх цифр, иначе
    # "от 4 до 6 млн" превратилось бы в 46
    text = re.sub(r"(?<=\d)\s+(?=\d{3}(?!\d))", "", text)
    numbers = [n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)?", text)]
    if not numbers:
        raise ValidationError("Не увидел суммы. Напишите цифрами, например: 5 млн")

    # для вилки "от 4 до 6 млн" берём нижнюю границу: это осторожная оценка
    value = float(numbers[0])

    for words, factor in _MULTIPLIERS:
        if any(word in text for word in words):
            value *= factor
            break

    amount = int(value)
    if amount < 100_000:
        raise ValidationError(
            "Сумма выглядит слишком маленькой. Укажите бюджет в рублях, например: 5 млн"
        )
    if amount > 10_000_000_000:
        raise ValidationError("Сумма выглядит нереалистичной, проверьте пожалуйста.")
    return amount


def parse_phone(raw: str) -> str:
    """Нормализует российский номер к виду +7XXXXXXXXXX."""
    digits = re.sub(r"\D", "", raw)

    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    else:
        raise ValidationError(
            "Не похоже на номер телефона. Пример: +7 999 123-45-67"
        )

    return "+" + digits


SCRIPT: tuple[Question, ...] = (
    Question(
        key="property_type",
        text="Что рассматриваете?",
        options=("Квартира", "Дом", "Коммерческая", "Участок"),
    ),
    Question(
        key="budget",
        text="На какой бюджет ориентируетесь? Можно примерно, например: 5 млн",
        parse=parse_budget,
    ),
    Question(
        key="timeline",
        text="Когда планируете покупку?",
        options=(
            "В течение месяца",
            "1-3 месяца",
            "3-6 месяцев",
            "Пока просто смотрю",
        ),
    ),
    Question(
        key="financing",
        text="Как планируете оплачивать?",
        options=("Свои средства", "Ипотека одобрена", "Ипотека нужна", "Ещё не решил"),
    ),
    Question(
        key="phone",
        text="Оставьте телефон, менеджер свяжется и подберёт варианты.",
        parse=parse_phone,
    ),
)


@dataclass
class Session:
    chat_id: int
    step: int = 0
    answers: dict[str, object] = field(default_factory=dict)
    finished: bool = False
    crm_id: str | None = None

    def snapshot(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "step": self.step,
            "answers": dict(self.answers),
            "finished": self.finished,
            "crm_id": self.crm_id,
        }

    @classmethod
    def restore(cls, raw: dict) -> "Session":
        return cls(
            chat_id=raw["chat_id"],
            step=raw.get("step", 0),
            answers=dict(raw.get("answers", {})),
            finished=raw.get("finished", False),
            crm_id=raw.get("crm_id"),
        )


@dataclass(frozen=True)
class Reply:
    text: str
    options: tuple[str, ...] = ()
    completed: bool = False


GREETING = (
    "Здравствуйте! Задам четыре коротких вопроса, чтобы подобрать варианты "
    "и передать вас нужному менеджеру."
)

DONE_TEXT = (
    "Спасибо, записал. Менеджер свяжется с вами в рабочее время. "
    "Если что-то поменяется, напишите сюда же."
)


class Dialogue:
    def __init__(self, script: tuple[Question, ...] = SCRIPT):
        if not script:
            raise ValueError("сценарий не может быть пустым")
        self.script = script

    def start(self) -> Reply:
        first = self.script[0]
        return Reply(f"{GREETING}\n\n{first.text}", first.options)

    def advance(self, session: Session, raw: str) -> Reply:
        """Обрабатывает один ответ и возвращает следующую реплику.

        Невалидный ответ не двигает шаг: вопрос задаётся повторно с подсказкой.
        """
        if session.finished:
            return Reply(DONE_TEXT, completed=True)

        question = self.script[session.step]
        try:
            value = question.handle(raw)
        except ValidationError as exc:
            return Reply(str(exc), question.options)

        session.answers[question.key] = value
        session.step += 1

        if session.step >= len(self.script):
            session.finished = True
            return Reply(DONE_TEXT, completed=True)

        nxt = self.script[session.step]
        return Reply(nxt.text, nxt.options)
