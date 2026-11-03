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
            "live_turns": 4, "round_size": 2, "shard_size": 2,
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
        self.assertEqual(chat["layers"]["archived"], 0)  # 5 < live_turns + round_size
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

    def test_condition_must_be_verbatim_and_model_cannot_retire_it(self):
        self.fill(["ห้ามใช้ wallet ในการคืนเงิน และต้องคืนภายใน 7 วัน"])
        with self.assertRaises(UserError):
            self.engine.add_state(self.chat, "condition", "ห้ามใช้กระเป๋าเงิน", source_tid=1)
        self.engine.add_state(self.chat, "condition", "ห้ามใช้ wallet ในการคืนเงิน", source_tid=1)
        cond = self.engine.get_chat(self.chat)["state"][0]
        self.fake.main_script = [tool_call("retire_state", item_id=cond["id"], reason="x"), {"content": "done"}]
        turn = self.engine.send(self.chat, "clean up")
        self.assertIn("only be removed by the user", turn["tools"][0]["result"])
        self.assertEqual(len(self.engine.get_chat(self.chat)["state"]), 1)

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


if __name__ == "__main__":
    unittest.main()
