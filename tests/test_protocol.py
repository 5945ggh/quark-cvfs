"""Synthetic protocol samples only; no real account data or compatibility claim."""
import json
import tempfile
import unittest
from pathlib import Path

from quark_vfs.provider.protocol import (
    Outcome,
    ProtocolError,
    assess_outcome,
    decode_static_code_status,
    parse_agent_identity,
    parse_ndjson,
    read_artifact_jsonl,
)


class ProtocolTests(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(parse_agent_identity("QK_AGENT_ID=codex\n"), "codex")
        for text in ("", "QK_AGENT_ID=other", "QK_AGENT_ID=codex\nwarning", '{"agent":"codex"}'):
            with self.assertRaises(ProtocolError):
                parse_agent_identity(text)

    def test_artifact_after_result_and_notice(self):
        stream = parse_ndjson('\n'.join(json.dumps(e) for e in [
            {"type": "progress"}, {"type": "list"},
            {"type": "result", "notice": {"text": "synthetic notice"}},
            {"type": "artifact", "data": {"file_path": "synthetic.jsonl"}},
        ]))
        self.assertEqual(len(stream.artifacts), 1)
        self.assertEqual(len(stream.notices), 1)
        self.assertEqual(assess_outcome(stream, 0), Outcome.UNKNOWN)

    def test_malformed_and_unknown(self):
        for text in ('no json', '[]', '{"type":"future"}',
                     '{"type":"result","type":"list"}',
                     '{"type":"result"}\n{"type":"result"}',
                     '{"type":"result","data":NaN}'):
            with self.assertRaises(ProtocolError):
                parse_ndjson(text)
        with self.assertRaises(ProtocolError):
            parse_ndjson('{"type":"list"}', max_line_bytes=2)

    def test_invalid_unicode_and_limits(self):
        with self.assertRaises(ProtocolError) as error:
            parse_ndjson('\ud800')
        self.assertEqual(error.exception.code, "INVALID_JSON")
        for name in ("max_bytes", "max_line_bytes"):
            for value in (-1, 0, True, 1.5):
                with self.assertRaises(ProtocolError):
                    parse_ndjson('', **{name: value})

    def test_unicode_filename_does_not_split_event(self):
        for separator in ('\u2028', '\u2029', '\u0085'):
            event = {"type": "list", "data": {"file_name": "sample" + separator + ".txt"}}
            stream = parse_ndjson(json.dumps(event, ensure_ascii=False) + '\r\n')
            self.assertEqual(stream.lists[0]["data"]["file_name"], event["data"]["file_name"])

    def test_artifact_invalid_options_fail_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            file = root / "nonempty.jsonl"
            file.write_text('{"fid":"synthetic"}\n')
            args = {"allowed_output_dir": root, "expected_count": 0, "required_fields": ("fid",)}
            for name in ("max_bytes", "max_line_bytes", "max_rows"):
                for value in (-1, 0, True, 1.5):
                    with self.assertRaises(ProtocolError) as error:
                        read_artifact_jsonl(file, **(args | {name: value}))
                    self.assertEqual(error.exception.code, "INVALID_LIMIT")
            for fields in ("fid", None, (1,), ("",), ([],)):
                with self.assertRaises(ProtocolError) as error:
                    read_artifact_jsonl(file, **(args | {"required_fields": fields}))
                self.assertEqual(error.exception.code, "INVALID_FIELDS")

    def test_exit_business_and_items(self):
        stream = parse_ndjson('{"type":"list"}\n{"type":"result"}')
        def decode(_):
            return True
        self.assertEqual(assess_outcome(stream, 0, result_decoder=decode), Outcome.UNKNOWN)
        self.assertEqual(assess_outcome(stream, 1, result_decoder=decode,
                                       item_decoder=lambda _: [True, False]), Outcome.PARTIAL)
        self.assertEqual(assess_outcome(stream, 0, result_decoder=lambda _: False,
                                       item_decoder=lambda _: [True]), Outcome.PARTIAL)
        self.assertEqual(assess_outcome(stream, 0, result_decoder=decode,
                                       item_decoder=lambda _: [True]), Outcome.SUCCESS)
        with self.assertRaises(ProtocolError):
            assess_outcome(stream, 0, result_decoder=lambda _: "true")
        self.assertEqual(assess_outcome(parse_ndjson(''), 1), Outcome.FAILED)

    def test_static_profile_rejects_bool_code(self):
        event = {"code": 0, "msg": "synthetic", "action": "browse", "data": {}}
        self.assertIs(decode_static_code_status(event), True)
        event["code"] = False
        self.assertIsNone(decode_static_code_status(event))

    def test_synthetic_fixture_full_collection(self):
        root = Path(__file__).parent / "fixtures" / "protocol"
        stream = parse_ndjson((root / "synthetic-browse.ndjson").read_text())
        artifact = stream.artifacts[0]["data"]
        rows = read_artifact_jsonl(artifact["file_path"], allowed_output_dir=root.resolve(),
                                   expected_count=artifact["count"], required_fields=("fid",))
        self.assertEqual(len(rows), 6)
        self.assertEqual(len(stream.results[0]["data"]["file_list"]), 1)

    def test_full_artifact_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            file = root / "synthetic.jsonl"
            file.write_text(''.join(json.dumps({"fid": str(i)}) + '\n' for i in range(105)))
            args = {"allowed_output_dir": root, "expected_count": 105, "required_fields": ("fid",)}
            self.assertEqual(len(read_artifact_jsonl(file, **args)), 105)
            for change in ({"expected_count": 5}, {"max_bytes": 10}, {"max_line_bytes": 2},
                           {"required_fields": ("missing",)}, {"max_rows": 100}):
                with self.assertRaises(ProtocolError):
                    read_artifact_jsonl(file, **(args | change))
            file.write_text('\n')
            with self.assertRaises(ProtocolError):
                read_artifact_jsonl(file, **args)

    def test_artifact_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "allowed"
            root.mkdir()
            outside = base / "outside.jsonl"
            outside.write_text('{"fid":"synthetic"}\n')
            (root / "link").symlink_to(outside)
            (root / "dirlink").symlink_to(base, target_is_directory=True)
            args = {"allowed_output_dir": root, "expected_count": 1, "required_fields": ("fid",)}
            for path in (outside, root / "link", root / "dirlink" / "outside.jsonl",
                         root / ".." / "outside.jsonl", root / "missing", root):
                with self.assertRaises(ProtocolError):
                    read_artifact_jsonl(path, **args)
            alias = base / "alias"
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ProtocolError):
                read_artifact_jsonl(alias / "link", **(args | {"allowed_output_dir": alias}))


if __name__ == "__main__":
    unittest.main()
