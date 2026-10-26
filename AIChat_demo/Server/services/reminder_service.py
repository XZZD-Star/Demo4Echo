"""Deskbot adapter for shared reminder deliveries."""

import time

import echomate_store
from services.notify_service import NotifyService
from tools import notify_store
from tools.logger import logger


class DeskbotReminderService:
    def __init__(self):
        self._last_play_at = 0.0
        self._hour_window_start = 0.0
        self._hour_count = 0

    def poll_once(self):
        # Reconcile deliveries whose notification has already been ACKed.
        for delivery in echomate_store.pending_deskbot_deliveries():
            dedupe_key = f"todo-delivery:{delivery['delivery_id']}"
            record = notify_store.find_by_dedupe_key(dedupe_key)
            if record and record.get("acked_at_utc"):
                echomate_store.acknowledge_delivery(delivery["delivery_id"])

        echomate_store.materialize_due_reminders()
        delivery = echomate_store.claim_delivery("deskbot")
        if not delivery:
            return False

        delivery_id = delivery["delivery_id"]
        todo_id = delivery["todo_id"]
        title = delivery["title"]

        notify_id = notify_store.enqueue(
            kind="todo",
            text=f"待办提醒：{title}",
            dedupe_key=f"todo-delivery:{delivery_id}",
            priority=5,
            ttl_minutes=120,
        )
        if notify_id is None:
            existing = notify_store.find_by_dedupe_key(
                f"todo-delivery:{delivery_id}"
            )
            if existing and existing["status"] == "sent":
                echomate_store.mark_delivery(delivery_id, "sent")
                echomate_store.finalize_todo_if_delivered(todo_id)
                return True
            # Existing pending/claimed notification will be handled by NotifyThread.
            echomate_store.mark_delivery(delivery_id, "sent")
            echomate_store.finalize_todo_if_delivered(todo_id)
            return True

        logger.info(
            "待办 #%s 已排入桌宠播报（delivery=%s）。",
            todo_id,
            delivery_id,
        )
        return True
