import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import async_session_factory
from ..models import IikoLink, SyncStatus, User
from .client import IikoClientError, IikoCloudClient, generate_card_number


async def sync_user_with_iiko(user_id: uuid.UUID, iiko: IikoCloudClient) -> None:
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            return
        result = await db.execute(select(IikoLink).where(IikoLink.user_id == user_id))
        link = result.scalar_one_or_none()
        if link is None:
            link = IikoLink(user_id=user_id, sync_status=SyncStatus.pending)
            db.add(link)

        try:
            existing = await iiko.find_guest_by_phone(user.phone, db=db)
            if existing:
                customer_id = existing.get("id") or existing.get("Id")
                existing_cards = existing.get("cards") or existing.get("Cards") or []
            else:
                created = await iiko.create_or_update_guest(
                    phone=user.phone, name=user.name, surname=user.surname, db=db
                )
                customer_id = created.get("id") or created.get("Id")
                existing_cards = []

            link.iiko_customer_id = customer_id

            if existing_cards:
                first_card = existing_cards[0]
                link.card_number = first_card.get("track") or first_card.get("Track") or first_card.get("number")
            else:
                card_number = generate_card_number()
                await iiko.add_card(customer_id=customer_id, card_number=card_number, phone=user.phone, db=db)
                link.card_number = card_number

            link.sync_status = SyncStatus.synced
            link.last_error = None
            link.linked_at = datetime.now(timezone.utc)
        except IikoClientError as exc:
            link.sync_status = SyncStatus.failed
            link.last_error = str(exc)[:1000]

        await db.commit()
