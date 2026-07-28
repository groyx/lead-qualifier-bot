"""Хранение сессий.

Без этого перезапуск бота теряет всех, кто не дошёл до конца анкеты: человек
ответил на четыре вопроса из пяти, бот перезапустился, и диалог начинается
заново. На практике такой лид просто уходит.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .dialogue import Session


class SessionStore:
    """Сессии в памяти со снимком на диск.

    Запись атомарная: сначала временный файл, потом fsync, потом замена.
    Падение посреди записи не оставляет обрезанный JSON, и старый файл
    остаётся читаемым.
    """

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else None
        self._sessions: dict[int, Session] = {}
        if self.path and self.path.exists():
            self._load()

    def get(self, chat_id: int) -> Session | None:
        return self._sessions.get(chat_id)

    def start(self, chat_id: int) -> Session:
        session = Session(chat_id=chat_id)
        self._sessions[chat_id] = session
        return session

    def drop(self, chat_id: int) -> None:
        self._sessions.pop(chat_id, None)

    def __len__(self) -> int:
        return len(self._sessions)

    def _load(self) -> None:
        assert self.path is not None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # битый файл не должен мешать боту стартовать
            return
        for item in raw.get("sessions", []):
            try:
                session = Session.restore(item)
            except (KeyError, TypeError):
                continue
            self._sessions[session.chat_id] = session

    def flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "sessions": [s.snapshot() for s in self._sessions.values()],
        }
        blob = json.dumps(payload, ensure_ascii=False, indent=2)

        handle, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
