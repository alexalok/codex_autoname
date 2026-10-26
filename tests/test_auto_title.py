import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import threading
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/auto_title.py"
spec = importlib.util.spec_from_file_location("auto_title", SCRIPT)
auto = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = auto
spec.loader.exec_module(auto)


def turn(number, status="completed", commentary=False):
    items = [{"id": f"u{number}", "type": "userMessage",
              "content": [{"type": "text", "text": f"Question {number}"}]}]
    if commentary:
        items.append({"id": f"c{number}", "type": "agentMessage", "text": "Working...", "phase": "commentary"})
        items.append({"id": f"tool{number}", "type": "commandExecution", "command": "private-command"})
    items.append({"id": f"a{number}", "type": "agentMessage",
                  "text": f"Answer {number}", "phase": "final_answer"})
    return {"id": f"t{number}", "status": status, "items": items}


class FakeRpc:
    def __init__(self, turns):
        self.history = turns
        self.name = "Default title"
        self.status = "idle"
        self.renames = []
        self.extra_metadata = {}
        self.lose_rename_reply = False

    def close(self):
        pass

    def call(self, method, params):
        if method == "thread/read":
            return {"thread": {"id": "chat", "name": self.name, "status": {"type": self.status},
                               "source": "appServer", "ephemeral": False, **self.extra_metadata}}
        if method == "thread/turns/list":
            data = self.history if params["sortDirection"] == "asc" else list(reversed(self.history))
            offset = int(params.get("cursor") or 0)
            count = min(params["limit"], 3)  # Force multiple pages in window tests.
            end = offset + count
            return {"data": copy.deepcopy(data[offset:end]),
                    "nextCursor": str(end) if end < len(data) else None}
        if method == "thread/name/set":
            self.name = params["name"]
            self.renames.append(params["name"])
            if self.lose_rename_reply:
                raise auto.RpcError("Lost reply")
            return {}
        raise AssertionError(method)


class FakeModel:
    def __init__(self):
        self.calls = []
        self.after_generate = None
        self.title = "Implement automatic chat titles"

    def generate(self, messages, initial):
        self.calls.append((copy.deepcopy(messages), initial))
        if self.after_generate:
            self.after_generate()
        return self.title


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = auto.Store(Path(self.directory.name))
        self.now = 10000
        self.config = auto.Config(enabled=True)
        self.rpc = FakeRpc([turn(1)])
        self.codex = auto.Codex(self.rpc)
        self.model = FakeModel()
        self.worker = auto.Worker(self.store, self.codex, self.model, self.config, clock=lambda: self.now)
        self.event("SessionStart", source="startup")
        self.event("UserPromptSubmit", turn_id="t1", prompt="Question 1")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def event(self, kind, **values):
        auto.record_event(self.store, {"session_id": "chat", "hook_event_name": kind, **values}, self.now)

    def stop(self, number=1):
        self.event("Stop", turn_id=f"t{number}", last_assistant_message=f"Answer {number}")

    def first_rename(self):
        self.stop()
        self.worker.tick()

    def next_turn(self):
        self.rpc.history.append(turn(2))
        self.event("UserPromptSubmit", turn_id="t2", prompt="Question 2")
        self.stop(2)

    def test_waits_for_first_successful_response(self):
        self.worker.tick()
        self.assertEqual([], self.model.calls)
        self.stop()
        self.rpc.history[-1]["status"] = "inProgress"
        self.worker.tick()
        self.assertEqual([], self.model.calls)
        self.rpc.history[-1]["status"] = "completed"
        self.worker.tick()
        self.assertEqual(1, len(self.rpc.renames))
        self.assertTrue(self.model.calls[0][1])
        self.assertEqual(["Question 1", "Answer 1"], [m["text"] for m in self.model.calls[0][0]])

    def test_requires_both_idle_and_one_hour_since_rename(self):
        self.first_rename()
        self.now += 60
        self.next_turn()
        self.now = 10700  # Idle ten minutes, but cooldown still active.
        self.worker.tick()
        self.assertEqual(1, len(self.model.calls))
        self.model.title = "Refine automatic chat titles"
        self.now = 13600
        self.worker.tick()
        self.assertEqual(2, len(self.rpc.renames))
        self.assertEqual(13600, self.store.get("chat").last_rename)

    def test_hour_cooldown_alone_is_insufficient(self):
        self.first_rename()
        self.now = 14000
        self.next_turn()
        self.now = 14599
        self.worker.tick()
        self.assertEqual(1, len(self.model.calls))
        self.now = 14600
        self.worker.tick()
        self.assertEqual(2, len(self.model.calls))

    def test_no_hourly_regeneration_without_content_changes(self):
        self.first_rename()
        self.now += 7200
        self.worker.tick()
        self.event("SessionStart", source="resume")
        self.now += 7200
        self.worker.tick()
        self.assertEqual(1, len(self.model.calls))

    def test_state_and_cooldown_survive_worker_restart(self):
        self.first_rename()
        self.now += 10
        self.next_turn()
        self.store.close()
        self.store = auto.Store(Path(self.directory.name))
        restarted = auto.Worker(self.store, self.codex, self.model, self.config, clock=lambda: self.now)
        self.now += 1200
        restarted.tick()
        self.assertEqual(1, len(self.model.calls))

    def test_new_user_input_while_generating_discards_title(self):
        self.stop()
        self.model.after_generate = lambda: self.event("UserPromptSubmit", turn_id="t2", prompt="Changed task")
        self.worker.tick()
        self.assertEqual([], self.rpc.renames)
        self.assertFalse(self.store.get("chat").initialized)

    def test_active_chat_is_not_idle(self):
        self.stop()
        self.rpc.status = "active"
        self.now += 7200
        self.worker.tick()
        self.assertEqual([], self.model.calls)

    def test_manual_change_pauses_automatic_updates(self):
        self.first_rename()
        self.rpc.name = "My chosen title"
        self.now += 7200
        self.next_turn()
        self.now += 601
        self.worker.tick()
        self.assertTrue(self.store.get("chat").paused)
        self.assertEqual(1, len(self.model.calls))

    def test_manual_change_during_model_call_is_preserved(self):
        self.stop()
        self.model.after_generate = lambda: setattr(self.rpc, "name", "Manual title")
        self.worker.tick()
        self.assertEqual([], self.rpc.renames)
        self.assertTrue(self.store.get("chat").paused)

    def test_failure_backs_off_without_marking_renamed(self):
        self.stop()
        self.model.generate = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Provider error"))
        self.worker.tick()
        record = self.store.get("chat")
        self.assertFalse(record.initialized)
        self.assertEqual(10030, record.retry_at)
        self.assertEqual(0, record.last_rename)

    def test_lost_rename_reply_is_reconciled_without_second_model_call(self):
        self.stop()
        self.rpc.lose_rename_reply = True
        with self.assertRaises(auto.RpcError):
            self.worker.tick()
        self.assertIsNotNone(self.store.get("chat").pending)
        self.now += 60
        self.worker.tick()
        record = self.store.get("chat")
        self.assertTrue(record.initialized)
        self.assertIsNone(record.pending)
        self.assertEqual(self.now, record.last_rename)
        self.assertEqual(1, len(self.model.calls))
        self.assertEqual(1, len(self.rpc.renames))

    def test_duplicate_hooks_do_not_reset_idle(self):
        self.stop()
        revision = self.store.get("chat").revision
        self.now += 100
        self.stop()
        self.assertEqual(revision, self.store.get("chat").revision)
        self.assertEqual(10000, self.store.get("chat").last_activity)

    def test_existing_and_forked_chats_are_not_enrolled(self):
        for source in ["resume", "compact", "fork"]:
            auto.record_event(self.store, {"session_id": source, "hook_event_name": "SessionStart",
                                          "source": source}, self.now)
            self.assertIsNone(self.store.get(source))

    def test_subagents_and_ephemeral_chats_are_ignored(self):
        self.stop()
        self.rpc.extra_metadata["parentThreadId"] = "parent"
        self.worker.tick()
        self.assertTrue(self.store.get("chat").paused)
        self.assertEqual([], self.model.calls)

    def test_dry_run_does_not_call_model_or_mutate_title_state(self):
        self.stop()
        self.worker.dry_run = True
        before = auto.asdict(self.store.get("chat"))
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.worker.tick()
        self.assertEqual(before, auto.asdict(self.store.get("chat")))
        self.assertEqual([], self.model.calls)
        self.assertEqual([], self.rpc.renames)
        self.assertEqual(2, json.loads(output.getvalue())["sample_messages"])

    def test_same_title_is_evaluated_once(self):
        self.first_rename()
        self.now += 7200
        self.next_turn()
        self.now += 601
        self.worker.tick()
        self.worker.tick()
        self.assertEqual(2, len(self.model.calls))
        self.assertEqual(1, len(self.rpc.renames))

    def test_later_turns_remain_pending_after_delayed_initial_title(self):
        self.stop()
        self.next_turn()
        self.worker.tick()
        self.assertEqual(["Question 1", "Answer 1"],
                         [m["text"] for m in self.model.calls[0][0]])
        self.now += 3600
        self.model.title = "Title including the second exchange"
        self.worker.tick()
        self.assertEqual(2, len(self.model.calls))
        self.assertFalse(self.model.calls[-1][1])
        self.assertIn("Question 2", [m["text"] for m in self.model.calls[-1][0]])
        self.worker.tick()
        self.assertEqual(2, len(self.model.calls))

    def test_later_turns_remain_pending_when_initial_title_matches(self):
        self.stop()
        self.next_turn()
        self.model.title = self.rpc.name
        self.worker.tick()
        self.assertEqual([], self.rpc.renames)
        self.now += 3600
        self.worker.tick()
        self.assertEqual(2, len(self.model.calls))
        self.assertIn("Question 2", [m["text"] for m in self.model.calls[-1][0]])

    def test_later_turns_remain_pending_after_initial_rename_recovery(self):
        self.stop()
        self.next_turn()
        self.rpc.lose_rename_reply = True
        with self.assertRaises(auto.RpcError):
            self.worker.tick()
        self.rpc.lose_rename_reply = False
        self.worker.tick()  # Reconcile the completed write; no second model request yet.
        self.now += 3600
        self.model.title = "Title including later content"
        self.worker.tick()
        self.assertEqual(2, len(self.model.calls))
        self.assertIn("Question 2", [m["text"] for m in self.model.calls[-1][0]])

    def test_interrupted_later_turn_gets_an_idle_update(self):
        self.first_rename()
        self.now += 60
        self.rpc.history.append(turn(2, status="interrupted", commentary=True))
        self.event("UserPromptSubmit", turn_id="t2", prompt="Question 2")
        self.event("Interrupt", turn_id="t2")
        self.now += 7200
        self.rpc.status = "active"
        self.worker.tick()
        self.assertEqual(1, len(self.model.calls))
        self.rpc.status = "idle"
        self.model.title = "Updated task after interruption"
        self.worker.tick()
        self.assertEqual(2, len(self.model.calls))
        self.assertFalse(self.model.calls[-1][1])
        self.assertIn("Question 2", [m["text"] for m in self.model.calls[-1][0]])

    def test_interrupted_first_turn_does_not_create_initial_title(self):
        self.rpc.history[-1]["status"] = "interrupted"
        self.event("Interrupt", turn_id="t1")
        self.now += 7200
        self.worker.tick()
        self.assertEqual([], self.model.calls)
        self.assertEqual([], self.rpc.renames)

    def test_unpause_honors_cooldown_before_initial_title(self):
        self.stop()
        self.model.after_generate = lambda: setattr(self.rpc, "name", "Manual title")
        self.worker.tick()
        self.assertTrue(self.store.get("chat").paused)
        self.assertFalse(self.store.get("chat").initialized)
        self.model.after_generate = None
        config_path = Path(self.directory.name) / "config.json"
        config_path.write_text(json.dumps({"enabled": True}))
        with patch.object(auto, "Rpc", return_value=self.rpc), \
             patch.object(auto.time, "time", return_value=self.now):
            self.assertEqual(0, auto.main(["--config", str(config_path),
                "--data-dir", self.directory.name, "unpause", "chat"]))
        self.worker.tick()
        self.assertEqual([], self.rpc.renames)
        self.now += 3599
        self.worker.tick()
        self.assertEqual([], self.rpc.renames)
        self.now += 1
        self.worker.tick()
        self.assertEqual(1, len(self.rpc.renames))

    def test_model_failures_are_bounded_per_conversation_revision(self):
        self.stop()
        attempts = []
        def fail(*args, **kwargs):
            attempts.append(True)
            raise RuntimeError("Provider failure")
        self.model.generate = fail
        for _ in range(8):
            self.worker.tick()
            self.now += 7200
        self.assertEqual(5, len(attempts))
        self.next_turn()
        self.worker.tick()
        self.assertEqual(6, len(attempts))


class SamplingTests(unittest.TestCase):
    def sample(self, count):
        return auto.Codex(FakeRpc([turn(i) for i in range(count)])).sample("chat", auto.Config(), False)

    def test_exact_first_ten_and_last_twenty(self):
        result = self.sample(40)
        self.assertEqual(30, len(result))
        self.assertEqual("Question 0", result[0]["text"])
        self.assertEqual("Answer 4", result[9]["text"])
        self.assertEqual("Question 30", result[10]["text"])
        self.assertEqual("Answer 39", result[-1]["text"])

    def test_overlapping_windows_are_deduplicated_in_order(self):
        for count in [1, 5, 8, 12, 15]:
            with self.subTest(count=count):
                result = self.sample(count)
                self.assertEqual(count * 2, len(result))
                self.assertEqual(count * 2, len({m["key"] for m in result}))
                self.assertEqual([f"{r} {n}" for n in range(count) for r in ["Question", "Answer"]],
                                 [m["text"] for m in result])

    def test_first_exchange_uses_final_response_and_excludes_tools(self):
        result = auto.Codex(FakeRpc([turn(1, commentary=True)])).sample("chat", auto.Config(), True)
        self.assertEqual(["Question 1", "Answer 1"], [m["text"] for m in result])

    def test_idle_sampling_counts_visible_commentary_but_not_tool_calls(self):
        result = auto.Codex(FakeRpc([turn(1, commentary=True)])).sample("chat", auto.Config(), False)
        self.assertEqual(["Question 1", "Working...", "Answer 1"], [m["text"] for m in result])

    def test_large_messages_are_capped_before_model_request(self):
        result = auto.prompt_messages([{"role": "user", "text": "X" * 10000}], 100)
        self.assertEqual("X" * 100 + "\n[message truncated]", result[0]["text"])


class ProviderTests(unittest.TestCase):
    def test_request_is_separate_no_tools_no_storage_and_configured_model(self):
        config = auto.Config(title_model="configured-model")
        model = auto.TitleModel(config)
        response = {"status": "completed", "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Automatic chat titles"}]}]}
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return io.BytesIO(json.dumps(response).encode())
        model.opener = Opener()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-only-key"}):
            title = model.generate([{"role": "user", "text": "Build titles"}], True)
        body = json.loads(model.opener.request.data)
        self.assertEqual("Automatic chat titles", title)
        self.assertEqual("configured-model", body["model"])
        self.assertFalse(body["store"])
        self.assertNotIn("tools", body)
        self.assertNotIn("previous_response_id", body)

    def test_invalid_or_multiline_titles_are_rejected(self):
        for value in ["", "X" * 81, "Title\nMore", "Title\x00"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                auto.normalize_title(value, 80)

    def test_https_required_except_for_local_model_endpoint(self):
        for url in ["http://external.example/v1/responses", "https://user:secret@example.com/v1/responses"]:
            with self.assertRaises(ValueError):
                auto.Config(responses_url=url).validate()
        auto.Config(responses_url="http://localhost:9000/v1/responses").validate()

    def test_codex_command_uses_native_ephemeral_auth_and_disables_integrations(self):
        with patch.object(auto, "configured_mcp_servers", return_value=["honcho", "camofox"]):
            command = auto.codex_title_command(auto.Config(title_model="configured-model"), Path("/tmp/title-test"))
        self.assertIn("--ephemeral", command)
        self.assertIn("--no-daemon", command)
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])
        self.assertNotIn("--ignore-user-config", command)
        self.assertNotIn("resume", command)
        self.assertIn("mcp_servers.honcho.enabled=false", command)
        self.assertIn("mcp_servers.camofox.enabled=false", command)
        self.assertIn('history.persistence="none"', command)
        self.assertIn("project_doc_max_bytes=0", command)
        self.assertIn("memories.use_memories=false", command)
        self.assertIn("--output-schema", command)

    def test_codex_backend_requires_completed_turn_and_schema_output(self):
        class Process:
            returncode = 0
            pid = 12345
            def __init__(self, command, **kwargs):
                self.command = command
                self.env = kwargs["env"]
            def communicate(self, prompt=None, timeout=None):
                self.prompt = prompt
                output = Path(self.command[self.command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"title": "Automatic conversation naming"}))
                return '{"type":"turn.completed"}\n', ""
        processes = []
        def spawn(*args, **kwargs):
            process = Process(*args, **kwargs)
            processes.append(process)
            return process
        config = auto.Config(title_backend="codex", title_model="configured-model")
        with patch.object(auto.subprocess, "Popen", side_effect=spawn), \
             patch.object(auto, "configured_mcp_servers", return_value=[]), \
             patch.dict("os.environ", {"CODEX_THREAD_ID": "parent", "CODEX_TURN_ID": "parent-turn"}):
            title = auto.TitleModel(config).generate([{"role": "user", "text": "Name this chat"}], True)
        self.assertEqual("Automatic conversation naming", title)
        self.assertNotIn("CODEX_THREAD_ID", processes[0].env)
        self.assertNotIn("CODEX_TURN_ID", processes[0].env)
        self.assertIn("Name this chat", processes[0].prompt)
        self.assertFalse(Path(processes[0].command[processes[0].command.index("--cd") + 1]).exists())

    def test_codex_backend_rejects_partial_success_exit(self):
        process = unittest.mock.Mock(returncode=0)
        process.communicate.return_value = ('{"type":"turn.started"}\n', "")
        config = auto.Config(title_backend="codex", title_model="configured-model")
        with patch.object(auto.subprocess, "Popen", return_value=process), \
             patch.object(auto, "configured_mcp_servers", return_value=[]), \
             self.assertRaisesRegex(RuntimeError, "did not complete"):
            auto.TitleModel(config).generate([], True)

    def test_codex_timeout_terminates_process_group(self):
        process = unittest.mock.Mock(pid=12345)
        process.communicate.side_effect = [auto.subprocess.TimeoutExpired("codex", 1), ("", "")]
        config = auto.Config(title_backend="codex", title_model="configured-model", model_timeout_seconds=1)
        with patch.object(auto.subprocess, "Popen", return_value=process), \
             patch.object(auto, "configured_mcp_servers", return_value=[]), \
             patch.object(auto.os, "killpg") as kill, \
             self.assertRaisesRegex(RuntimeError, "timed out"):
            auto.TitleModel(config).generate([], True)
        kill.assert_called_once_with(12345, auto.signal.SIGTERM)


class TransportTests(unittest.TestCase):
    def test_real_subprocess_handshake_and_notification_interleaving(self):
        adapter = Path(__file__).with_name("fake_app_server.py")
        config = auto.Config(app_server_command=[sys.executable, "-u", str(adapter)])
        rpc = auto.Rpc(config)
        try:
            self.assertEqual([], rpc.call("thread/list", {})["data"])
            with self.assertRaises(auto.RpcCallError):
                rpc.call("unsupported/method", {})
            self.assertEqual([], rpc.call("thread/list", {})["data"])
        finally:
            rpc.close()

    def test_disconnected_subprocess_does_not_hang(self):
        adapter = Path(__file__).with_name("fake_app_server.py")
        config = auto.Config(app_server_command=[sys.executable, "-u", str(adapter), "disconnect"])
        with self.assertRaises(auto.RpcError):
            auto.Rpc(config)

    def test_end_to_end_unix_websocket_and_local_title_provider(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from websockets.sync.server import unix_serve
        fake = FakeRpc([turn(1)])
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                payload = json.dumps({"status": "completed", "output": [{"type": "message",
                    "role": "assistant", "content": [{"type": "output_text",
                    "text": "Automatic chat title worker"}]}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        def handle_socket(connection):
            for raw in connection:
                message = json.loads(raw)
                if "id" not in message:
                    continue
                if message["method"] == "initialize":
                    result = {"userAgent": "synthetic-test-server"}
                else:
                    result = fake.call(message["method"], message["params"])
                connection.send(json.dumps({"id": message["id"], "result": result}))
        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "server.sock")
            http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            http_thread = threading.Thread(target=http.serve_forever, daemon=True)
            http_thread.start()
            with unix_serve(handle_socket, socket_path) as server:
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                config = auto.Config(enabled=True, app_server_socket=socket_path,
                    title_model="synthetic-title-model", api_key_env="AUTO_TITLE_TEST_UNUSED_KEY",
                    responses_url=f"http://127.0.0.1:{http.server_port}/v1/responses")
                rpc = auto.Rpc(config)
                store = auto.Store(Path(directory) / "state")
                try:
                    auto.record_event(store, {"session_id": "chat", "hook_event_name": "SessionStart",
                                              "source": "startup"}, 10000)
                    auto.record_event(store, {"session_id": "chat", "hook_event_name": "Stop",
                                              "turn_id": "t1"}, 10001)
                    worker = auto.Worker(store, auto.Codex(rpc), auto.TitleModel(config),
                                         config, clock=lambda: 10002)
                    worker.tick()
                    worker.tick()
                    self.assertEqual(["Automatic chat title worker"], fake.renames)
                    self.assertEqual(1, len(requests))
                    self.assertEqual("synthetic-title-model", requests[0]["model"])
                    self.assertEqual(10002, store.get("chat").last_rename)
                finally:
                    store.close()
                    rpc.close()
                    server.shutdown()
                    server_thread.join(timeout=2)
            http.shutdown()
            http.server_close()
            http_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
