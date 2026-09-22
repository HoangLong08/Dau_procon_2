"""File sự kiện cho web trực quan hoá: ghi từng dòng, đọc tiếp từ offset, bỏ dòng ghi dở."""

import os
import tempfile
import unittest

from hexudon.viz.events import NULL_EMIT, EventWriter, log_level, read_events


class EventsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "sub", "events.jsonl")

    def test_written_events_are_read_back_in_order_with_bot_and_time(self):
        clock = iter([100.0, 101.5])
        writer = EventWriter(self.path, clock=lambda: next(clock))
        writer.for_bot(None)("match_start", {"mode": "offline"})
        writer.for_bot("1. search 3s")("submit", {"day": 0, "ok": True})
        writer.close()
        events, offset = read_events(self.path)
        self.assertEqual([e["seq"] for e in events], [0, 1])
        self.assertEqual([e["kind"] for e in events], ["match_start", "submit"])
        self.assertIsNone(events[0]["bot"])
        self.assertEqual(events[1]["bot"], "1. search 3s")
        self.assertEqual(events[1]["t"], 101.5)
        self.assertEqual(events[1]["data"], {"day": 0, "ok": True})
        self.assertEqual(offset, os.path.getsize(self.path))

    def test_a_half_written_last_line_waits_for_the_next_read(self):
        writer = EventWriter(self.path)
        writer.write(None, "log", {"text": "một"})
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('{"seq": 1, "kind": "lo')
        events, offset = read_events(self.path)
        self.assertEqual(len(events), 1)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('g", "data": {}}\n')
        more, offset2 = read_events(self.path, offset)
        self.assertEqual([e["kind"] for e in more], ["log"])
        self.assertEqual(offset2, os.path.getsize(self.path))
        writer.close()

    def test_a_broken_line_is_skipped_not_fatal(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('không phải json\n{"seq": 0, "kind": "log", "data": {}}\n')
        events, _ = read_events(self.path)
        self.assertEqual([e["kind"] for e in events], ["log"])

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(read_events(self.path, 0), ([], 0))

    def test_unserialisable_data_is_written_as_text_not_raised(self):
        writer = EventWriter(self.path)
        writer.write("x", "log", {"obj": object()})
        writer.close()
        writer.write("x", "log", {"sau khi đóng": True})     # không ném lỗi, không ghi gì
        events, _ = read_events(self.path)
        self.assertEqual(len(events), 1)

    def test_null_emit_and_log_levels(self):
        self.assertIsNone(NULL_EMIT("log", {}))
        self.assertEqual(log_level("Ngày 6/10: bài bị từ chối (HTTP 0)"), "warn")
        self.assertEqual(log_level("ngày 0 đội x: bài KHÔNG hợp lệ (E_NO_FUEL)"), "warn")
        self.assertEqual(log_level("LỆCH LUẬT ở 1/2 ngày"), "warn")
        self.assertEqual(log_level("Ngày 2/10: quá hạn, không gửi nữa"), "warn")
        self.assertEqual(log_level("chọn vai trò xong trong 3.0s"), "info")
