"""Offline tests for the Q1 trigger-parser hardening (Spec E).

No network and no real model call: replies are neutral placeholder strings written by THIS test ("lorem"
reasoning, fixed enum codes). No board or chatter content is read, printed, or asserted.

    cd lab && python3 -m unittest study.test_triggers_hardening
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from study import modelio, triggers as tg
from study.test_instigation import TRIPWIRE, make_fixture

THINK = "<think>\nlorem ipsum {not json} dolor [1, 2\n</think>\n"
FENCED = '```json\n{"codes": [{"code": "EMPTY_BOARD", "confidence": 0.8}]}\n```'


def codes(rec):
    return [(c["code"], c["confidence"]) for c in rec["codes"]]


class TolerantExtraction(unittest.TestCase):
    def v(self, text):
        return tg.validate_response(text, "r", 0, "qwen38")

    def test_think_then_fenced_json(self):
        self.assertEqual(codes(self.v(THINK + FENCED)), [("EMPTY_BOARD", 0.8)])

    def test_orphan_close_think(self):
        self.assertEqual(codes(self.v("lorem {x} ipsum</think>" + FENCED)), [("EMPTY_BOARD", 0.8)])

    def test_inline_fence_and_prose(self):
        text = 'Lorem ipsum: ```json {"codes": [{"code": "TASK_HINT", "confidence": 1}]}``` dolor.'
        self.assertEqual(codes(self.v(text)), [("TASK_HINT", 1.0)])

    def test_prose_prefers_answer_shaped_object(self):
        text = 'lorem {"a": 1} ipsum {"codes": [{"code": "UNPROMPTED", "confidence": 0.3}]} dolor'
        self.assertEqual(codes(self.v(text)), [("UNPROMPTED", 0.3)])

    def test_bare_list_of_codes_gets_default_confidence(self):
        rec = self.v('["SAW_PEER_POSTS", "direct_question "]')
        self.assertEqual(codes(rec), [("SAW_PEER_POSTS", tg.DEFAULT_CONFIDENCE),
                                      ("DIRECT_QUESTION", tg.DEFAULT_CONFIDENCE)])

    def test_single_object(self):
        self.assertEqual(codes(self.v('{"code": "NONE", "confidence": "0.25"}')), [("NONE", 0.25)])

    def test_codes_as_strings_and_single_string(self):
        self.assertEqual(codes(self.v('{"codes": ["CALL_TO_ACTION"]}')),
                         [("CALL_TO_ACTION", tg.DEFAULT_CONFIDENCE)])
        self.assertEqual(codes(self.v('{"codes": "REPLY_TO_SELF"}')),
                         [("REPLY_TO_SELF", tg.DEFAULT_CONFIDENCE)])

    def test_duplicates_collapse_to_max(self):
        text = '{"codes": [{"code": "NONE", "confidence": 0.2}, {"code": "NONE", "confidence": 0.6}]}'
        self.assertEqual(codes(self.v(text)), [("NONE", 0.6)])

    def test_unknown_code_still_schema_error_without_content(self):
        text = THINK + '```json\n{"codes": [{"code": "INJECTED_' + TRIPWIRE + '", "confidence": 1}]}\n```'
        with self.assertRaises(tg.SchemaError) as cm:
            self.v(text)
        e = cm.exception
        self.assertEqual(e.code, "E_SCHEMA")
        self.assertGreaterEqual(e.line, 4)  # position is in the ORIGINAL text (think lines preserved)
        self.assertNotIn(TRIPWIRE, json.dumps(e.as_dict()))
        self.assertNotIn(TRIPWIRE, str(e))

    def test_invalid_confidences_are_schema_errors(self):
        for conf in ('true', '1.5', '-0.1', '"high"', 'null'):
            text = '{"codes": [{"code": "NONE", "confidence": %s}]}' % conf
            if conf == 'null':  # explicit null == absent -> default (documented)
                self.assertEqual(codes(self.v(text)), [("NONE", tg.DEFAULT_CONFIDENCE)])
                continue
            with self.assertRaises(tg.SchemaError) as cm:
                self.v(text)
            self.assertEqual(cm.exception.code, "E_SCHEMA", conf)

    def test_empty_and_wrong_shapes(self):
        for text in ('{"codes": []}', '[]', '{"lorem": 1}', '42', '[1]'):
            with self.assertRaises(tg.SchemaError) as cm:
                self.v(text)
            self.assertEqual(cm.exception.code, "E_SCHEMA", text)

    def test_only_reasoning_or_truncated_is_parse_error(self):
        for text in (THINK, '<think>lorem {"codes": ["NONE"]} ipsum', "", "lorem ipsum"):
            with self.assertRaises(tg.SchemaError) as cm:
                self.v(text)
            e = cm.exception
            self.assertEqual(e.code, "E_PARSE")
            self.assertGreaterEqual(e.line, 1)
            self.assertGreaterEqual(e.col, 1)

    def test_truncated_reply_is_parse_error_not_fragment(self):
        """A reply cut off mid-list must not yield the first inner object (dropping the tail's codes)."""
        for text in ('{"codes":[{"code":"TASK_HINT","confidence":0.3},{"code":"CALL_TO_ACTION","confidence":0.9',
                     '{"codes":[{"code":"TASK_HINT","confidence":0.3},{"code":"CALL_TO',
                     'lorem {"codes":[{"code":"TASK_HINT","confidence":0.3},{"code":"NONE","confidence":tr',
                     '{"codes":[{"code":"TASK_HINT","confidence":0.3},\n'):
            with self.assertRaises(tg.SchemaError) as cm:
                self.v(text)
            self.assertEqual(cm.exception.code, "E_PARSE")

    def test_stray_bracket_before_answer_skipped(self):
        text = 'Step [1] lorem. {"codes":[{"code":"TASK_HINT","confidence":0.9}]}'
        self.assertEqual(codes(self.v(text)), [("TASK_HINT", 0.9)])
        self.assertEqual(codes(self.v('lorem [1, 2] ipsum ["TASK_HINT"]')), [("TASK_HINT", 0.5)])
        # an unbalanced brace in prose is not mistaken for truncation
        self.assertEqual(codes(self.v('lorem { ipsum. {"codes":["NONE"]}')), [("NONE", 0.5)])
        # an answer object nested in a wrapper object is still found
        self.assertEqual(codes(self.v('lorem {"result": {"codes": ["NONE"]}}')), [("NONE", 0.5)])

    def test_parser_version_bumped(self):
        self.assertGreaterEqual(tg.PARSER_VERSION, 2)
        self.assertEqual(self.v(FENCED)["parser_version"], tg.PARSER_VERSION)


class RetryOnParse(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = make_fixture(Path(tmp.name))

    def test_single_retry_recovers(self):
        replies = iter(["<think>lorem", THINK + FENCED])
        calls = []

        def stub(sp, uc, slug):
            calls.append(1)
            return next(replies)

        art = tg.run(self.run_dir, agents=[0], model_slug="qwen38", call_model=stub)
        self.assertEqual(len(calls), 2)
        self.assertEqual(codes(art["records"][0]), [("EMPTY_BOARD", 0.8)])
        raw = self.run_dir / "instigation" / "raw"
        self.assertTrue((raw / "a0.txt").is_file())
        self.assertTrue((raw / "a0.r1.txt").is_file())

    def test_retry_exhausted_records_parse_error(self):
        calls = []

        def stub(sp, uc, slug):
            calls.append(1)
            return "lorem ipsum"

        art = tg.run(self.run_dir, agents=[0], model_slug="qwen38", call_model=stub)
        self.assertEqual(len(calls), tg.PARSE_RETRIES + 1)
        self.assertEqual(art["records"][0]["error_code"], "E_PARSE")

    def test_schema_error_not_retried(self):
        calls = []

        def stub(sp, uc, slug):
            calls.append(1)
            return '{"codes": ["NOT_A_CODE"]}'

        art = tg.run(self.run_dir, agents=[0], model_slug="qwen38", call_model=stub)
        self.assertEqual(len(calls), 1)
        self.assertEqual(art["records"][0]["error_code"], "E_SCHEMA")


class RecordingTransport:
    """Duck-types modelio.Transport.chat; records kwargs; may reject response_format once."""

    def __init__(self, content, reject_json_mode=False):
        self.content, self.reject = content, reject_json_mode
        self.kws = []

    def chat(self, messages, **kw):
        self.kws.append(dict(kw))
        if self.reject and kw.get("response_format"):
            raise modelio.ModelError("E_HTTP")
        return {"choices": [{"message": {"content": self.content}}]}


class DefaultCallPath(unittest.TestCase):
    """The NON-injected path: triggers.run -> _default_call_model -> modelio.complete -> transport."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = make_fixture(Path(tmp.name))
        self._orig = modelio.make_transport
        self.addCleanup(setattr, modelio, "make_transport", self._orig)
        tg._JSON_OBJECT_REJECTED.clear()
        self.addCleanup(tg._JSON_OBJECT_REJECTED.clear)
        # exercise the JSON-mode path on the default slug; its real config turns JSON mode off
        # (see test_qwen38_default_path_is_plain_mode)
        import config
        patcher = mock.patch.dict(config.MODELS["qwen38"], {"json_object": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_think_wrapped_fenced_reply_yields_enum_code_and_json_mode(self):
        tp = RecordingTransport(THINK + FENCED)
        modelio.make_transport = lambda *a, **k: tp  # no network / no key
        art = tg.run(self.run_dir, agents=[0], model_slug="qwen38")
        rec = art["records"][0]
        self.assertNotIn("error_code", rec)
        self.assertIn(rec["codes"][0]["code"], tg.ENUM_SET)
        self.assertEqual(rec["codes"][0]["code"], "EMPTY_BOARD")
        self.assertEqual(len(tp.kws), 1)
        self.assertEqual(tp.kws[0]["response_format"], {"type": "json_object"})
        self.assertEqual(tp.kws[0]["temperature"], tg.CLASSIFIER_TEMPERATURE)

    def test_json_mode_rejected_falls_back_without_it(self):
        tp = RecordingTransport(FENCED, reject_json_mode=True)
        modelio.make_transport = lambda *a, **k: tp
        art = tg.run(self.run_dir, agents=[0, 1], model_slug="qwen38")
        self.assertTrue(all("error_code" not in r for r in art["records"]))
        # agent 0: rejected json-mode call + plain call; agent 1: plain only (rejection remembered)
        self.assertEqual([bool(k.get("response_format")) for k in tp.kws], [True, False, False])

    def test_capability_is_per_provider(self):
        import config
        self.assertTrue(tg.supports_json_object("qwen38"))        # model flag patched on in setUp
        self.assertFalse(tg.supports_json_object("mistral24"))    # berget: not declared
        self.assertFalse(tg.supports_json_object("no_such_slug"))
        with mock.patch.dict(config.MODELS["qwen38"]):
            del config.MODELS["qwen38"]["json_object"]
            self.assertTrue(tg.supports_json_object("qwen38"))    # falls back to the provider (cerebras)

    def test_qwen38_default_path_is_plain_mode(self):
        """Real config: qwen38 loops in its reasoning channel under JSON mode, so the default call path
        sends no response_format and a large completion budget, and still yields an enum code."""
        mock.patch.stopall()
        tp = RecordingTransport(THINK + FENCED)
        modelio.make_transport = lambda *a, **k: tp
        self.assertFalse(tg.supports_json_object("qwen38"))
        art = tg.run(self.run_dir, agents=[0], model_slug="qwen38")
        self.assertEqual(art["records"][0]["codes"][0]["code"], "EMPTY_BOARD")
        self.assertIsNone(tp.kws[0].get("response_format"))
        self.assertEqual(tp.kws[0]["max_tokens"], tg.CLASSIFIER_MAX_TOKENS)

    def test_non_http_model_error_propagates(self):
        class Boom:
            def chat(self, messages, **kw):
                raise modelio.ModelError("E_AUTH")
        modelio.make_transport = lambda *a, **k: Boom()
        with self.assertRaises(modelio.ModelError):
            tg._default_call_model("s", "u", "qwen38")


if __name__ == "__main__":
    unittest.main()
