"""Связывание частей: входящее сообщение -> диалог -> скоринг -> CRM."""

from __future__ import annotations

import logging

from .crm import CRMClient, CRMRejected, CRMUnavailable, Lead
from .dialogue import Dialogue, Session
from .scoring import score
from .storage import SessionStore
from .transport import Transport

log = logging.getLogger("leadbot")

RESTART_COMMANDS = {"/start", "/restart", "начать", "заново"}

CRM_FAILED_TEXT = (
    "Записал ваши ответы. С отправкой в систему возникла заминка, "
    "менеджер всё равно свяжется с вами."
)


class LeadBot:
    def __init__(
        self,
        transport: Transport,
        crm: CRMClient,
        store: SessionStore | None = None,
        dialogue: Dialogue | None = None,
    ):
        self.transport = transport
        self.crm = crm
        # именно "is not None": пустой SessionStore ложен по истинности,
        # потому что у него определён __len__, и `store or ...` тихо
        # подменял бы переданное хранилище новым, без файла
        self.store = store if store is not None else SessionStore()
        self.dialogue = dialogue if dialogue is not None else Dialogue()

    async def handle(self, chat_id: int, text: str) -> None:
        command = text.strip().casefold()
        session = self.store.get(chat_id)

        if command in RESTART_COMMANDS or session is None:
            session = self.store.start(chat_id)
            reply = self.dialogue.start()
            self.store.flush()
            await self.transport.send(chat_id, reply.text, reply.options)
            return

        if session.finished:
            # анкета уже сдана: не заводим дубль, просто подтверждаем
            await self.transport.send(
                chat_id,
                "Ваша заявка уже у менеджера. Напишите /start, если нужно заполнить заново.",
            )
            return

        reply = self.dialogue.advance(session, text)
        self.store.flush()

        if not reply.completed:
            await self.transport.send(chat_id, reply.text, reply.options)
            return

        await self.transport.send(chat_id, reply.text)
        await self._push_to_crm(session)

    async def _push_to_crm(self, session: Session) -> None:
        lead_score = score(session.answers)
        lead = Lead(
            chat_id=session.chat_id,
            phone=str(session.answers.get("phone", "")),
            answers=dict(session.answers),
            score=lead_score,
        )

        try:
            crm_id = await self.crm.create_lead(lead)
        except (CRMUnavailable, CRMRejected) as exc:
            # ответы уже сохранены, лид не теряется: его видно в сессиях
            log.error("лид %s не ушёл в CRM: %s", session.chat_id, exc)
            await self.transport.send(session.chat_id, CRM_FAILED_TEXT)
            return

        session.crm_id = crm_id
        self.store.flush()
        log.info(
            "лид %s создан в CRM как %s (%s, %d баллов)",
            session.chat_id,
            crm_id,
            lead_score.temperature,
            lead_score.points,
        )
