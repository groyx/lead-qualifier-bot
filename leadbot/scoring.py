"""Скоринг лида.

Правила вынесены в таблицы, а не зашиты в условия: отдел продаж меняет пороги
без правки логики. Каждое начисление баллов сопровождается причиной, поэтому
менеджер в CRM видит, почему лид горячий, а не просто цифру.
"""

from __future__ import annotations

from dataclasses import dataclass, field

HOT = "hot"
WARM = "warm"
COLD = "cold"

TIMELINE_POINTS = {
    "В течение месяца": (50, "покупка в течение месяца"),
    "1-3 месяца": (35, "покупка в горизонте 1-3 месяцев"),
    "3-6 месяцев": (15, "покупка в горизонте 3-6 месяцев"),
    "Пока просто смотрю": (0, "сроки не определены"),
}

FINANCING_POINTS = {
    "Ипотека одобрена": (30, "ипотека уже одобрена"),
    "Свои средства": (25, "покупка за свои средства"),
    "Ипотека нужна": (10, "нужна помощь с ипотекой"),
    "Ещё не решил": (0, "способ оплаты не определён"),
}

# нижняя граница бюджета -> баллы; порядок от большего к меньшему
BUDGET_TIERS = (
    (15_000_000, 30, "бюджет от 15 млн"),
    (8_000_000, 20, "бюджет от 8 млн"),
    (4_000_000, 10, "бюджет от 4 млн"),
    (0, 0, "бюджет ниже 4 млн"),
)

HOT_THRESHOLD = 75
WARM_THRESHOLD = 40


@dataclass(frozen=True)
class Score:
    points: int
    temperature: str
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_hot(self) -> bool:
        return self.temperature == HOT


def _budget_points(budget: object) -> tuple[int, str]:
    if not isinstance(budget, (int, float)):
        return 0, "бюджет не указан"
    for floor, points, reason in BUDGET_TIERS:
        if budget >= floor:
            return points, reason
    return 0, "бюджет не указан"


def score(answers: dict) -> Score:
    points = 0
    reasons: list[str] = []

    for key, table in (("timeline", TIMELINE_POINTS), ("financing", FINANCING_POINTS)):
        value = answers.get(key)
        gained, reason = table.get(value, (0, None))
        points += gained
        if reason:
            reasons.append(reason)

    gained, reason = _budget_points(answers.get("budget"))
    points += gained
    reasons.append(reason)

    if points >= HOT_THRESHOLD:
        temperature = HOT
    elif points >= WARM_THRESHOLD:
        temperature = WARM
    else:
        temperature = COLD

    return Score(points=points, temperature=temperature, reasons=tuple(reasons))
