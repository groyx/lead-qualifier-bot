"""Отправка сообщений в Telegram.

Транспорт спрятан за протоколом: тесты гоняют весь диалог через MockTransport
и читают, что именно бот ответил, не поднимая ни бота, ни сеть.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol


class Transport(Protocol):
    async def send(
        self, chat_id: int, text: str, options: tuple[str, ...] = ()
    ) -> None: ...


@dataclass
class Sent:
    chat_id: int
    text: str
    options: tuple[str, ...] = ()


class MockTransport:
    """Складывает отправленное в список вместо реальной отсылки."""

    def __init__(self) -> None:
        self.messages: list[Sent] = []

    async def send(
        self, chat_id: int, text: str, options: tuple[str, ...] = ()
    ) -> None:
        self.messages.append(Sent(chat_id, text, options))

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [
            m.text for m in self.messages if chat_id is None or m.chat_id == chat_id
        ]

    @property
    def last(self) -> Sent | None:
        return self.messages[-1] if self.messages else None


@dataclass
class RateLimiter:
    """Ограничитель под лимиты Telegram: 1 сообщение в секунду на чат
    и не больше 30 в секунду суммарно.

    Часы инжектятся, поэтому тест проверяет расчёт паузы мгновенно,
    не засыпая по-настоящему.
    """

    per_chat_interval: float = 1.0
    global_limit: int = 30
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _chat_last: dict[int, float] = field(default_factory=dict)
    _window: list[float] = field(default_factory=list)

    async def acquire(self, chat_id: int) -> None:
        wait = self._delay_for(chat_id)
        if wait > 0:
            await self.sleep(wait)
        stamp = self.now() + max(wait, 0.0)
        self._chat_last[chat_id] = stamp
        self._window.append(stamp)

    def _delay_for(self, chat_id: int) -> float:
        current = self.now()
        self._window[:] = [t for t in self._window if current - t < 1.0]

        wait = 0.0
        last = self._chat_last.get(chat_id)
        if last is not None:
            wait = max(wait, last + self.per_chat_interval - current)

        if len(self._window) >= self.global_limit:
            oldest = min(self._window)
            wait = max(wait, oldest + 1.0 - current)

        return wait


class TelegramTransport:
    """Реальная отправка через Bot API. HTTP инжектится ради тестируемости."""

    def __init__(
        self,
        token: str,
        http: Callable[[str, dict], Awaitable[dict]] | None = None,
        limiter: RateLimiter | None = None,
    ):
        self.base = f"https://api.telegram.org/bot{token}"
        self._http = http or _urllib_post
        self.limiter = limiter or RateLimiter()

    async def send(
        self, chat_id: int, text: str, options: tuple[str, ...] = ()
    ) -> None:
        await self.limiter.acquire(chat_id)
        payload: dict = {"chat_id": chat_id, "text": text}
        if options:
            payload["reply_markup"] = json.dumps(
                {
                    "keyboard": [[{"text": o}] for o in options],
                    "resize_keyboard": True,
                    "one_time_keyboard": True,
                },
                ensure_ascii=False,
            )
        else:
            payload["reply_markup"] = json.dumps({"remove_keyboard": True})
        await self._http(f"{self.base}/sendMessage", payload)


async def _urllib_post(url: str, payload: dict) -> dict:
    def call() -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    return await asyncio.to_thread(call)
