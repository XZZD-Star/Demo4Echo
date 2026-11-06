import asyncio
import importlib.util
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = ROOT.parents[1] / "echomate_core"
SERVER_ROOT = ROOT
MAIN_PATH = SERVER_ROOT / "main.py"

sys.path.insert(0, str(CORE_ROOT))
sys.path.insert(0, str(SERVER_ROOT))

import echomate_store
import sqlite_db
from tools import flow_log
from tools import memory_store
from tools import notify_store


class SqliteArchitectureTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "assistant.db"
        echomate_store.DB_PATH = self.db_path
        memory_store.DB_PATH = self.db_path
        notify_store.DB_PATH = self.db_path
        echomate_store.init_schema()
        memory_store.init_memory_tables()
        notify_store.init_notify_tables()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _insert_due_todo(self):
        due_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(timespec="seconds")
        with echomate_store.write_connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO todos
                    (owner_id, title, due_at_utc, timezone, status,
                     created_at_utc, chat_id, source, dedupe_key,
                     notify_deskbot, notify_feishu)
                VALUES
                    ('owner', 'test reminder', ?, 'Asia/Shanghai', 'pending',
                     ?, 'chat', 'test', 'test-dedupe', 1, 1)
                """,
                (due_at, echomate_store.now_utc()),
            )
            return cursor.lastrowid

    def test_materialize_is_idempotent(self):
        todo_id = self._insert_due_todo()

        self.assertEqual(echomate_store.materialize_due_reminders(), 2)
        self.assertEqual(echomate_store.materialize_due_reminders(), 0)

        with echomate_store.connect() as conn:
            rows = conn.execute(
                """
                SELECT channel, status FROM reminder_deliveries
                WHERE todo_id = ? ORDER BY channel
                """,
                (todo_id,),
            ).fetchall()
        self.assertEqual(
            [(row["channel"], row["status"]) for row in rows],
            [("deskbot", "pending"), ("feishu", "pending")],
        )

    def test_ack_updates_outbox_and_delivery_in_one_transaction(self):
        self._insert_due_todo()
        echomate_store.materialize_due_reminders()
        delivery = echomate_store.claim_delivery("deskbot")
        self.assertIsNotNone(delivery)

        notify_id = notify_store.enqueue(
            "todo",
            "test reminder",
            dedupe_key=f"todo-delivery:{delivery['delivery_id']}",
        )
        notify_store.mark_sent(notify_id)
        self.assertEqual(notify_store.mark_acked([notify_id]), 1)

        with echomate_store.connect() as conn:
            outbox = conn.execute(
                "SELECT acked_at_utc FROM notify_outbox WHERE id = ?",
                (notify_id,),
            ).fetchone()
            reminder = conn.execute(
                """
                SELECT status, acked_at_utc FROM reminder_deliveries
                WHERE id = ?
                """,
                (delivery["delivery_id"],),
            ).fetchone()
        self.assertIsNotNone(outbox["acked_at_utc"])
        self.assertEqual(reminder["status"], "sent")
        self.assertIsNotNone(reminder["acked_at_utc"])

    def test_materialize_expires_claimed_delivery_after_reminder_handled(self):
        todo_id = self._insert_due_todo()
        echomate_store.materialize_due_reminders()
        delivery = echomate_store.claim_delivery("deskbot")
        self.assertIsNotNone(delivery)

        old_due = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat(timespec="seconds")
        with echomate_store.write_connect() as conn:
            conn.execute(
                "UPDATE todos SET due_at_utc = ?, reminded_at_utc = ? WHERE id = ?",
                (old_due, echomate_store.now_utc(), todo_id),
            )

        echomate_store.materialize_due_reminders()
        with echomate_store.connect() as conn:
            row = conn.execute(
                """
                SELECT status, last_error FROM reminder_deliveries
                WHERE id = ?
                """,
                (delivery["delivery_id"],),
            ).fetchone()
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["last_error"], "reminder expired")

    def test_write_retries_after_lock_is_released(self):
        old_timeout = sqlite_db.DEFAULT_TIMEOUT_SECONDS
        sqlite_db.DEFAULT_TIMEOUT_SECONDS = 0.1
        lock_conn = sqlite3.connect(
            str(self.db_path), timeout=0.1, check_same_thread=False
        )
        lock_conn.execute("BEGIN EXCLUSIVE")

        def release_lock():
            time.sleep(0.6)
            lock_conn.rollback()
            lock_conn.close()

        releaser = threading.Thread(target=release_lock)
        releaser.start()
        try:
            notify_store.set_setting("retry-test", "ok")
        finally:
            releaser.join()
            sqlite_db.DEFAULT_TIMEOUT_SECONDS = old_timeout

        self.assertEqual(notify_store.get_setting("retry-test"), "ok")

    def test_memory_candidate_uses_shared_connection_layer(self):
        candidate_id, result = memory_store.enqueue_candidate(
            "owner", "喜欢简短回答"
        )
        self.assertEqual(result, "created")
        self.assertTrue(
            memory_store.decide_candidate("owner", candidate_id, "accept")
        )
        self.assertEqual(
            memory_store.list_candidates("owner"),
            [],
        )

    def test_chat_memory_search_and_delete_are_scoped(self):
        first_id, first_result = echomate_store.add_chat_memory(
            "owner",
            "用户最近在准备挑战杯比赛",
            session_id="session-a",
            source_turn_id="turn-a",
        )
        second_id, second_result = echomate_store.add_chat_memory(
            "owner",
            "用户喜欢周末散步",
            session_id="session-b",
            source_turn_id="turn-b",
        )
        self.assertEqual(first_result, "created")
        self.assertEqual(second_result, "created")
        self.assertNotEqual(first_id, second_id)

        matches = echomate_store.search_chat_memories(
            "owner",
            "挑战杯进展怎么样",
            exclude_session_id="session-current",
        )
        self.assertEqual([item["id"] for item in matches], [first_id])
        self.assertTrue(
            echomate_store.delete_chat_memory("owner", first_id)
        )
        self.assertEqual(
            echomate_store.list_chat_memories("owner")[0]["id"],
            second_id,
        )

    def test_preference_upsert_updates_same_key(self):
        first_id, first_result = echomate_store.upsert_preference_memory(
            "owner",
            "用户喜欢先听结论",
            "response_style",
            "voice_auto",
            confidence=0.9,
        )
        second_id, second_result = echomate_store.upsert_preference_memory(
            "owner",
            "用户喜欢先听结论，再补充细节",
            "response_style",
            "voice_auto",
            confidence=0.95,
        )
        self.assertEqual(first_result, "created")
        self.assertEqual(second_id, first_id)
        self.assertEqual(second_result, "updated")
        with echomate_store.connect() as conn:
            row = conn.execute(
                """
                SELECT content, evidence_count, confidence
                FROM memories WHERE id = ?
                """,
                (first_id,),
            ).fetchone()
        self.assertIn("补充细节", row["content"])
        self.assertEqual(row["evidence_count"], 2)
        self.assertAlmostEqual(row["confidence"], 0.95)

    def test_assistant_actions_dispatch_shared_read_tools(self):
        from services import assistant_actions

        due_at = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(timespec="seconds")
        todo_id, result = echomate_store.create_todo(
            "owner",
            "共享工具测试",
            due_at,
            source="voice",
        )
        self.assertEqual(result, "created")
        response = assistant_actions.execute_tool_call(
            "list_todos",
            "{}",
            owner_id="owner",
            source="voice",
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["items"][0]["id"], todo_id)

    def test_playback_failure_is_not_acked_or_reclaimed(self):
        self._insert_due_todo()
        echomate_store.materialize_due_reminders()
        delivery = echomate_store.claim_delivery("deskbot")
        self.assertIsNotNone(delivery)
        notify_id = notify_store.enqueue(
            "todo",
            "test reminder",
            dedupe_key=f"todo-delivery:{delivery['delivery_id']}",
        )
        claimed = notify_store.claim_one(["todo"])
        self.assertEqual(claimed["id"], notify_id)
        notify_store.mark_sent(notify_id)

        self.assertEqual(
            notify_store.mark_playback_failed(
                [notify_id], "client playback timeout"
            ),
            1,
        )
        self.assertIsNone(notify_store.claim_one(["todo"]))

        with echomate_store.connect() as conn:
            outbox = conn.execute(
                """
                SELECT status, acked_at_utc, last_error
                FROM notify_outbox WHERE id = ?
                """,
                (notify_id,),
            ).fetchone()
            reminder = conn.execute(
                """
                SELECT status, acked_at_utc, last_error
                FROM reminder_deliveries WHERE id = ?
                """,
                (delivery["delivery_id"],),
            ).fetchone()
        self.assertEqual(outbox["status"], "failed")
        self.assertIsNone(outbox["acked_at_utc"])
        self.assertIn("timeout", outbox["last_error"])
        self.assertEqual(reminder["status"], "failed")
        self.assertIsNone(reminder["acked_at_utc"])

    def test_flow_log_filters_events(self):
        old_path = flow_log.DEFAULT_PATH
        path = Path(self.temp_dir.name) / "flow.jsonl"
        flow_log.DEFAULT_PATH = path
        try:
            flow_log.emit(
                "notify", "claimed", "info",
                trace_id="notify-151", notify_id=151,
            )
            flow_log.emit(
                "tts", "error", "error",
                trace_id="notify-151", notify_id=151,
                message="denied",
            )
            events = flow_log.read_events(
                limit=20, notify_id=151, component="tts"
            )
        finally:
            flow_log.DEFAULT_PATH = old_path
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "error")

    def test_worker_watchdog_requests_fail_fast(self):
        module_names = (
            "ws_server",
            "threads",
            "threads.tts_thread",
            "threads.audio_send_thread",
            "threads.notify_thread",
            "tools.logger",
            "service_manager",
        )
        previous = {name: sys.modules.get(name) for name in module_names}

        ws_server = types.ModuleType("ws_server")
        ws_server.WebSocketServer = object
        tts_thread = types.ModuleType("threads.tts_thread")
        tts_thread.TTSGenerateThread = object
        audio_thread = types.ModuleType("threads.audio_send_thread")
        audio_thread.AudioSendThread = object
        notify_thread = types.ModuleType("threads.notify_thread")
        notify_thread.NotifyThread = object
        tools_logger = types.ModuleType("tools.logger")

        class LoggerStub:
            def error(self, *args, **kwargs):
                pass

            def info(self, *args, **kwargs):
                pass

        tools_logger.logger = LoggerStub()
        service_manager_module = types.ModuleType("service_manager")
        service_manager_module.ServiceManager = object

        for name, module in (
            ("ws_server", ws_server),
            ("threads", types.ModuleType("threads")),
            ("threads.tts_thread", tts_thread),
            ("threads.audio_send_thread", audio_thread),
            ("threads.notify_thread", notify_thread),
            ("tools.logger", tools_logger),
            ("service_manager", service_manager_module),
        ):
            sys.modules[name] = module

        try:
            spec = importlib.util.spec_from_file_location("stage_main", MAIN_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            class ManagerStub:
                def __init__(self):
                    self.stop_event = threading.Event()
                    self.reason = None

                def fail_fast(self, reason):
                    self.reason = reason
                    self.stop_event.set()

            manager = ManagerStub()
            dead_thread = threading.Thread(target=lambda: None, name="dead-worker")
            asyncio.run(module.monitor_workers(manager, [dead_thread], 0.01))
        finally:
            for name, old_module in previous.items():
                if old_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old_module

        self.assertEqual(manager.reason, "dead-worker 已停止")


if __name__ == "__main__":
    unittest.main()
