"""Отправка лида в CRM.

Клиент спрятан за протоколом, поэтому весь путь проверяется на мок-сервисе
без обращения к боевому Битриксу. Поверх любого клиента вешается обёртка
с ретраями и защитой от дублей.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from .scoring import Score


class CRMError(Exception):
    """Базовая ошибка CRM."""


class CRMUnavailable(CRMError):
    """Временный отказ: сеть, 5xx, таймаут. Имеет смысл повторить."""


class CRMRejected(CRMError):
    """CRM отвергла данные: невалидное поле, нет прав. Повтор не поможет."""


@dataclass(frozen=True)
class Lead:
    chat_id: int
    phone: str
    answers: dict
    score: Score
    source: str = "Telegram"

    def title(self) -> str:
        kind = self.answers.get("property_type", "Недвижимость")
        return f"{kind}, {self.score.temperature.upper()} ({self.score.points})"

    def comment(self) -> str:
        lines = [f"Источник: {self.source}", ""]
        for key, label in (
            ("property_type", "Тип"),
            ("budget", "Бюджет"),
            ("timeline", "Срок"),
            ("financing", "Оплата"),
        ):
            value = self.answers.get(key)
            if value is None:
                continue
            if key == "budget" and isinstance(value, (int, float)):
                value = f"{int(value):,}".replace(",", " ") + " ₽"
            lines.append(f"{label}: {value}")
        lines.append("")
        lines.append(f"Скоринг: {self.score.points} ({self.score.temperature})")
        lines.extend(f"  - {r}" for r in self.score.reasons)
        return "\n".join(lines)

    def dedup_key(self) -> str:
        """Один и тот же лид от одного чата не должен заводиться дважды."""
        payload = json.dumps(
            {"chat_id": self.chat_id, "phone": self.phone},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class CRMClient(Protocol):
    async def create_lead(self, lead: Lead) -> str:
        """Возвращает идентификатор созданного лида."""
        ...


class MockCRM:
    """Мок-сервис для тестов и локального прогона.

    Умеет притворяться недоступной заданное число раз подряд, чтобы можно было
    проверить поведение ретраев, не поднимая настоящий Битрикс.
    """

    def __init__(self, fail_times: int = 0, error: type[CRMError] = CRMUnavailable):
        self.created: list[Lead] = []
        self.fail_times = fail_times
        self.error = error
        self.attempts = 0

    async def create_lead(self, lead: Lead) -> str:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error("мок-сервис недоступен")
        self.created.append(lead)
        return f"mock-{len(self.created)}"


HttpPost = Callable[[str, dict], Awaitable[dict]]


async def _urllib_post(url: str, fields: dict) -> dict:
    """POST формы через стандартную библиотеку, чтобы не тянуть зависимости."""

    def call() -> dict:
        body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    return await asyncio.to_thread(call)


class Bitrix24CRM:
    """Адаптер входящего вебхука Битрикс24 (crm.lead.add).

    HTTP-вызов инжектится, поэтому адаптер тестируется подменой транспорта.
    """

    def __init__(self, webhook_url: str, http: HttpPost | None = None):
        self.webhook_url = webhook_url.rstrip("/")
        self._http = http or _urllib_post

    async def create_lead(self, lead: Lead) -> str:
        fields = {
            "fields[TITLE]": lead.title(),
            "fields[SOURCE_ID]": "TELEGRAM",
            "fields[COMMENTS]": lead.comment(),
            "fields[PHONE][0][VALUE]": lead.phone,
            "fields[PHONE][0][VALUE_TYPE]": "MOBILE",
            "fields[OPPORTUNITY]": lead.answers.get("budget", 0),
            "fields[CURRENCY_ID]": "RUB",
        }
        try:
            payload = await self._http(f"{self.webhook_url}/crm.lead.add.json", fields)
        except CRMError:
            raise
        except Exception as exc:  # сеть, таймаут, битый JSON
            raise CRMUnavailable(f"запрос к Битриксу не прошёл: {exc}") from exc

        if "error" in payload:
            description = payload.get("error_description", payload["error"])
            raise CRMRejected(f"Битрикс отклонил лид: {description}")

        result = payload.get("result")
        if not result:
            raise CRMRejected(f"неожиданный ответ Битрикса: {payload}")
        return str(result)


class RetryingCRM:
    """Ретраи с экспоненциальным ростом паузы плюс защита от дублей.

    Дубли возникают буднично: пользователь дважды жмёт кнопку, или ответ CRM
    теряется по таймауту уже после создания лида. Ключ дедупликации не даёт
    завести вторую карточку на тот же телефон из того же чата.
    """

    def __init__(
        self,
        inner: CRMClient,
        attempts: int = 3,
        base_delay: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        seen: dict[str, str] | None = None,
    ):
        if attempts < 1:
            raise ValueError("attempts должен быть не меньше 1")
        self.inner = inner
        self.attempts = attempts
        self.base_delay = base_delay
        self._sleep = sleep
        self.seen: dict[str, str] = seen if seen is not None else {}

    async def create_lead(self, lead: Lead) -> str:
        key = lead.dedup_key()
        if key in self.seen:
            return self.seen[key]

        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                crm_id = await self.inner.create_lead(lead)
            except CRMRejected:
                raise  # повтор не поможет, данные плохие
            except CRMUnavailable as exc:
                last = exc
                if attempt == self.attempts - 1:
                    break
                delay = self.base_delay * (2**attempt)
                await self._sleep(delay + random.uniform(0, delay * 0.1))
            else:
                self.seen[key] = crm_id
                return crm_id

        raise CRMUnavailable(f"CRM недоступна после {self.attempts} попыток: {last}")
