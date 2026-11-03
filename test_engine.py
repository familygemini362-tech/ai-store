"""Offline tests with a fake OpenRouter client. Run: python -m unittest -v test_engine"""

import json
import re
import tempfile
import unittest
from pathlib import Path

import prompts
from engine import Engine, Meter, UserError


class FakeClient:
    """Answers per role based on the system prompt. `main_script` is a queue of main-model replies."""

    def __init__(self):
        self.main_script = []
        self.fail_scan_ids = set()  # a shard containing one of these IDs always returns garbage
        self.calls = []

    def __call__(self, api_key):
        return self

    def chat(self, model, messages, tools=None, tool_choice=None):
        system = messages[0]["content"]
        user = messages[-1]["content"] if messages[-1]["role"] == "user" else ""
        self.calls.append(system[:20])
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001}
        if system == prompts.SUMMARIZE_SYSTEM:
            ids = [int(x) for x in re.search(r"Summarize turn IDs ([\d, ]+)\.", user).group(1).split(",")]
            out = []
            for i in ids:
                body = re.search(rf'<turn id="{i}">\nuser: (.*?)\n', user).group(1)
                out.append({"id": i, "user": body, "answer": f"answer {i}", "omitted": ""})
            return {"content": json.dumps({"summaries": out})}, usage
        if system == prompts.SCAN_SYSTEM:
            shard_ids = [int(x) for x in re.findall(r"^ID (\d+) \|", user, re.M)]
            if self.fail_scan_ids & set(shard_ids):
                return {"content": "not json"}, usage
            query = re.search(r"<query>\n(.*)\n</query>", user, re.S).group(1)
            words = set(re.findall(r"\w+", query.lower()))
            hits = [i for i in shard_ids
                    if words & set(re.findall(r"\w+", re.search(rf"^ID {i} \|(.*)$", user, re.M).group(1).lower()))]
            status = "found" if hits else "not_found"
            return {"content": json.dumps({"status": status, "ids": hits, "reason": "test"})}, usage
        if self.main_script:
            return self.main_script.pop(0), usage
        return {"content": "ok"}, usage


def tool_call(name, **args):
    return {"content": "", "tool_calls": [
        {"id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeClient()
        self.engine = Engine(Path(self.tmp.name) / "t.db", client_factory=self.fake, summarize_async=False)
        self.engine.save_settings({
            "api_key": "test-key", "model_summarizer": "s", "model_scanner": "c", "model_main": "m",
            "compress_at": 6, "round_size": 2, "shard_size": 2,
        })
        self.chat = self.engine.create_chat()

    def tearDown(self):
        self.tmp.cleanup()

    def fill(self, texts):
        for t in texts:
            self.engine.send(self.chat, t)

    def test_summarizes_oldest_round_as_separate_ids(self):
        self.fill(["apple", "banana", "cherry", "durian", "elder"])
        chat = self.engine.get_chat(self.chat)
        self.assertEqual(chat["layers"]["archived"], 0)  # 5 < compress_at (6)
        self.fill(["fig"])
        chat = self.engine.get_chat(self.chat)
        self.assertEqual(chat["layers"]["archived"], 2)
        self.assertEqual(chat["layers"]["live"], 4)
        self.assertEqual(chat["turns"][0]["summary"]["user"], "apple")
        self.assertEqual(chat["turns"][1]["summary"]["user"], "banana")

    def test_live_turns_are_sent_and_archived_are_not(self):
        self.fill(["apple", "banana", "cherry", "durian", "elder", "fig"])
        seen = []
        orig = self.fake.chat

        def spy(model, messages, **kw):
            if messages[0]["content"] == prompts.MAIN_SYSTEM:
                seen.append(messages)
            return orig(model, messages, **kw)
        self.fake.chat = spy
        self.engine.send(self.chat, "grape")
        contents = [m["content"] for m in seen[0][1:-1] if m["role"] == "user"]
        self.assertEqual(contents, ["[ID 3] cherry", "[ID 4] durian", "[ID 5] elder", "[ID 6] fig"])
        self.assertIn("<memory_state>", seen[0][-1]["content"])

    def test_scan_shards_report_every_status_and_retry_failed(self):
        self.fill(["apple", "banana", "cherry", "durian", "elder", "fig", "grape", "kiwi"])
        # archived IDs 1-4 -> shards [1,2] and [3,4]
        self.fake.fail_scan_ids = {3}
        result = self.engine.scan(self.chat, 9, "banana", self.engine.get_settings(), Meter())
        by_range = {tuple(s["range"]): s for s in result["shards"]}
        self.assertEqual(by_range[(1, 2)]["status"], "found")
        self.assertEqual(by_range[(1, 2)]["ids"], [2])
        self.assertEqual(by_range[(3, 4)]["status"], "failed")
        self.assertEqual(by_range[(3, 4)]["attempts"], 2)
        state = self.engine.get_chat(self.chat)["state"]
        self.assertEqual([(s["kind"], s["source_tid"]) for s in state], [("summary", 2)])

    def test_hint_word_forces_scan(self):
        self.fill(["apple", "banana", "cherry", "durian", "elder", "fig"])
        turn = self.engine.send(self.chat, "ที่เคยคุยเรื่อง apple ไว้")
        self.assertEqual(len(turn["meta"]["scans"]), 1)
        self.assertEqual(turn["meta"]["scans"][0]["trigger"], "reference")
        self.assertEqual(turn["meta"]["scans"][0]["ids"], [1])
        plain = self.engine.send(self.chat, "hello")
        self.assertEqual(plain["meta"]["scans"], [])

    def test_main_tool_loop_search_then_read_full(self):
        self.fill(["apple", "banana", "cherry", "durian", "elder", "fig"])
        self.fake.main_script = [
            tool_call("search_memory", query="apple"),
            tool_call("read_full", ids=[1]),
            {"content": "final answer"},
        ]
        turn = self.engine.send(self.chat, "what fruit first")
        self.assertEqual(turn["answer"], "final answer")
        self.assertEqual([t["name"] for t in turn["tools"]], ["search_memory", "read_full"])
        state = self.engine.get_chat(self.chat)["state"]
        # read_full replaced the summary of ID 1 with its full text
        self.assertEqual([(s["kind"], s["source_tid"]) for s in state], [("full", 1)])

    def test_note_must_cite_live_id(self):
        self.fill(["apple", "banana", "cherry", "durian", "elder", "fig"])
        self.fake.main_script = [tool_call("add_note", text="x", source_id=1), {"content": "done"}]
        turn = self.engine.send(self.chat, "note it")
        self.assertIn("not in layer 1", turn["tools"][0]["result"])
        self.fake.main_script = [tool_call("add_note", text="y", source_id=5), {"content": "done"}]
        turn = self.engine.send(self.chat, "note it again")
        self.assertIn("Added", turn["tools"][0]["result"])

    def test_condition_must_be_verbatim_and_model_cannot_retire_it_unasked(self):
        self.fill(["ห้ามใช้ wallet ในการคืนเงิน และต้องคืนภายใน 7 วัน"])
        with self.assertRaises(UserError):
            self.engine.add_state(self.chat, "condition", "ห้ามใช้กระเป๋าเงิน", source_tid=1)
        self.engine.add_state(self.chat, "condition", "ห้ามใช้ wallet ในการคืนเงิน", source_tid=1)
        cond = self.engine.get_chat(self.chat)["state"][0]
        self.fake.main_script = [tool_call("retire_state", item_id=cond["id"], reason="x"), {"content": "done"}]
        turn = self.engine.send(self.chat, "clean up")
        self.assertIn("is in 4a", turn["tools"][0]["result"])
        # A quote that is not in the user's current message does not count as the user asking.
        self.fake.main_script = [tool_call("retire_state", item_id=cond["id"], reason="x",
                                           user_quote="ยกเลิกเงื่อนไข wallet"), {"content": "done"}]
        turn = self.engine.send(self.chat, "tidy layer 4")
        self.assertIn("not a verbatim quote", turn["tools"][0]["result"])
        self.assertEqual(len(self.engine.get_chat(self.chat)["state"]), 1)

    def test_model_pins_condition_from_users_exact_words(self):
        self.fake.main_script = [
            tool_call("pin_condition", text="ตอบเป็นภาษาไทยเสมอ", source_id=1),
            tool_call("pin_condition", text="always answer in Thai", source_id=1),  # paraphrase/translation
            tool_call("pin_condition", text="ตอบเป็นภาษาไทยเสมอ", source_id=1),  # duplicate
            {"content": "ok"},
        ]
        turn = self.engine.send(self.chat, "ช่วยวางแผนทริป  ตอบเป็นภาษาไทยเสมอ นะ")
        results = [t["result"] for t in turn["tools"]]
        self.assertTrue(results[0].startswith("Pinned as [C"))
        self.assertIn("not a verbatim quote", results[1])
        self.assertIn("Already pinned", results[2])
        state = self.engine.get_chat(self.chat)["state"]
        self.assertEqual([(s["kind"], s["text"], s["origin"]) for s in state],
                         [("condition", "ตอบเป็นภาษาไทยเสมอ", "model")])

    def test_model_replaces_and_retires_condition_when_user_asks(self):
        self.fake.main_script = [tool_call("pin_condition", text="งบไม่เกิน 5000 บาท", source_id=1), {"content": "ok"}]
        self.engine.send(self.chat, "งบไม่เกิน 5000 บาท")
        old = self.engine.get_chat(self.chat)["state"][0]
        self.fake.main_script = [
            tool_call("pin_condition", text="งบไม่เกิน 8000 บาท", source_id=2, replaces=old["id"]), {"content": "ok"}]
        self.engine.send(self.chat, "เปลี่ยนเป็น งบไม่เกิน 8000 บาท")
        state = self.engine.get_chat(self.chat)["state"]
        self.assertEqual([s["text"] for s in state], ["งบไม่เกิน 8000 บาท"])
        self.fake.main_script = [tool_call("retire_state", item_id=state[0]["id"], reason="user cancelled",
                                           user_quote="ไม่ต้องจำกัดงบแล้ว"), {"content": "ok"}]
        turn = self.engine.send(self.chat, "ไม่ต้องจำกัดงบแล้ว ครับ")
        self.assertTrue(turn["tools"][0]["result"].startswith("Retired [C"))
        self.assertEqual(self.engine.get_chat(self.chat)["state"], [])

    def test_over_cap_trims_oldest_retrieved_items_only(self):
        self.fill(["apple pie", "apple tart", "cherry", "durian", "elder", "fig"])
        self.engine.add_state(self.chat, "condition", "apple pie", source_tid=1)
        self.engine.send(self.chat, "ที่เคยคุยเรื่อง apple")  # retrieves summaries of IDs 1 and 2
        kinds = sorted(s["kind"] for s in self.engine.get_chat(self.chat)["state"])
        self.assertEqual(kinds, ["condition", "summary", "summary"])
        with self.engine.db() as con:
            base = len(self.engine._render_state(con, self.chat, 99, self.engine.get_settings(), []))
        self.engine.save_settings({"state_cap_chars": base - 20})
        self.engine.send(self.chat, "hello")  # over the cap: retrieved summaries go first, 4a stays
        kinds = [s["kind"] for s in self.engine.get_chat(self.chat)["state"]]
        self.assertIn("condition", kinds)
        self.assertLess(kinds.count("summary"), 2)

    def test_streamed_send_emits_events_in_order(self):
        fake = self.fake

        def chat_stream(model, messages, on_text=None, **kw):
            msg, usage = fake.chat(model, messages, **kw)
            for piece in (msg.get("content") or "").split(" "):
                if piece:
                    on_text(piece + " ")
            return msg, usage
        fake.chat_stream = chat_stream
        fake.main_script = [tool_call("add_note", text="step 1", source_id=1), {"content": "hello there"}]
        events = []
        turn = self.engine.send(self.chat, "hi", on_event=events.append)
        types = [e["type"] for e in events]
        self.assertEqual(types[:2], ["status", "round"])
        self.assertIn("tool", types)
        self.assertEqual(types.count("round"), 2)
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta"), "hello there ")
        self.assertEqual(turn["answer"], "hello there")
        self.assertNotIn(self.chat, self.engine._listeners)

    def test_decision_replaces_older_one(self):
        self.fill(["use 24h", "ok"])
        self.fake.main_script = [tool_call("record_decision", text="claim window 24h", source_id=2), {"content": "ok"}]
        self.engine.send(self.chat, "agreed")
        old = self.engine.get_chat(self.chat)["state"][0]
        self.fake.main_script = [
            tool_call("record_decision", text="claim window 48h", source_id=4, replaces=old["id"]), {"content": "ok"}]
        self.engine.send(self.chat, "change to 48h")
        state = self.engine.get_chat(self.chat)["state"]
        self.assertEqual([s["text"] for s in state], ["claim window 48h"])

    def test_retrieved_summary_expires(self):
        self.engine.save_settings({"summary_ttl": 1})
        self.fill(["apple", "banana", "cherry", "durian", "elder", "fig"])
        self.engine.send(self.chat, "ที่เคยคุยเรื่อง apple")  # ID 7, summary expires after ID 8
        self.assertEqual(len(self.engine.get_chat(self.chat)["state"]), 1)
        self.fill(["x"])  # ID 8 still sees it
        self.assertEqual(len(self.engine.get_chat(self.chat)["state"]), 1)
        self.fill(["y"])  # ID 9 expires it
        self.assertEqual(len(self.engine.get_chat(self.chat)["state"]), 0)

    def test_budget_stops_tool_loop(self):
        self.engine.save_settings({"max_usd_per_message": 0.0015})
        self.fill(["apple", "banana", "cherry", "durian", "elder", "fig"])
        self.fake.main_script = [tool_call("search_memory", query="apple")] * 5
        turn = self.engine.send(self.chat, "loop")
        self.assertEqual(turn["meta"]["stopped"], "budget")

    def test_missing_model_is_a_user_error(self):
        self.engine.save_settings({"model_main": ""})
        with self.assertRaises(UserError):
            self.engine.send(self.chat, "hi")

    def test_per_chat_override_compresses_independently(self):
        chat2 = self.engine.create_chat()
        self.engine.set_compression(self.chat, 4, 1)  # compress sooner, one at a time
        self.fill(["a", "b", "c", "d"])
        for t in ["a", "b", "c", "d"]:
            self.engine.send(chat2, t)  # chat2 keeps using the global settings (6 / 2)
        c1 = self.engine.get_chat(self.chat)
        c2 = self.engine.get_chat(chat2)
        self.assertEqual(c1["layers"]["archived"], 1)
        self.assertEqual(c1["layers"]["live"], 3)
        self.assertEqual(c2["layers"]["archived"], 0)

    def test_clearing_override_reverts_to_global(self):
        self.engine.set_compression(self.chat, 3, 1)
        comp = self.engine.get_chat(self.chat)["compression"]
        self.assertEqual((comp["compress_at"], comp["round_size"]), (3, 1))
        self.engine.set_compression(self.chat, None, None)
        comp = self.engine.get_chat(self.chat)["compression"]
        self.assertEqual((comp["compress_at"], comp["round_size"]), (6, 2))
        self.assertIsNone(comp["custom"]["compress_at"])
        self.assertIsNone(comp["custom"]["round_size"])

    def test_compression_validation_rejects_bad_values(self):
        with self.assertRaises(UserError):
            self.engine.set_compression(self.chat, 3, 3)  # round_size must be < compress_at
        with self.assertRaises(UserError):
            self.engine.set_compression(self.chat, 1, 1)  # compress_at must be >= 2
        with self.assertRaises(UserError):
            self.engine.save_settings({"round_size": 10})  # >= current compress_at (6)
        with self.assertRaises(UserError):
            self.engine.save_settings({"compress_at": 1})

    def test_live_turns_migration(self):
        with self.engine.db() as con:
            con.execute("DELETE FROM settings WHERE key IN ('compress_at', 'round_size', 'live_turns')")
            con.execute("INSERT INTO settings(key, value) VALUES('live_turns', '10')")
            con.execute("INSERT INTO settings(key, value) VALUES('round_size', '3')")
        self.assertEqual(self.engine.get_settings()["compress_at"], 13)

    def test_get_chat_compression_shape(self):
        chat = self.engine.get_chat(self.chat)
        self.assertEqual(chat["compression"], {
            "compress_at": 6, "round_size": 2,
            "custom": {"compress_at": None, "round_size": None},
            "default": {"compress_at": 6, "round_size": 2},
        })
        self.assertEqual(chat["layers"]["next_summary_at"], 6)


if __name__ == "__main__":
    unittest.main()
