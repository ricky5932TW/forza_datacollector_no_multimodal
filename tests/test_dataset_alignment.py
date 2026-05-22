import unittest

from capture_dataset import TELEMETRY_COLUMNS, build_dataset_rows, nearest_packet


def frame(frame_id: int, t_ns: int) -> dict[str, str]:
    return {
        "frame_id": str(frame_id),
        "image_path": f"images/{frame_id:06d}.jpg",
        "t_present_perf_ns": str(t_ns),
    }


def packet(packet_id: int, t_ns: int, parse_error: str = "") -> dict[str, str]:
    row = {
        "packet_id": str(packet_id),
        "t_recv_perf_ns": str(t_ns),
        "packet_size": "324",
        "parse_error": parse_error,
    }
    for column in TELEMETRY_COLUMNS:
        row[column] = ""
    row["Speed"] = "42.5"
    row["Steer"] = "-7"
    row["Accel"] = "200"
    row["Brake"] = "0"
    return row


class DatasetAlignmentTests(unittest.TestCase):
    def test_nearest_packet(self) -> None:
        self.assertIsNone(nearest_packet([], 100))
        self.assertEqual(nearest_packet([90, 130], 100), 0)
        self.assertEqual(nearest_packet([90, 130], 125), 1)

    def test_valid_nearest_packet_row(self) -> None:
        rows = build_dataset_rows([frame(0, 1_000_000_000)], [packet(7, 1_010_000_000)])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["packet_id"], "7")
        self.assertEqual(rows[0]["packet_dt_ms"], "10.000")
        self.assertEqual(rows[0]["is_valid"], 1)
        self.assertEqual(rows[0]["Speed"], "42.5")
        self.assertEqual(rows[0]["Steer"], "-7")

    def test_far_packet_is_invalid_but_retains_telemetry(self) -> None:
        rows = build_dataset_rows(
            [frame(0, 1_000_000_000)],
            [packet(7, 1_040_000_000)],
            max_packet_gap_ms=25,
        )

        self.assertEqual(rows[0]["is_valid"], 0)
        self.assertEqual(rows[0]["packet_dt_ms"], "40.000")
        self.assertEqual(rows[0]["Speed"], "42.5")

    def test_parse_error_blanks_telemetry(self) -> None:
        rows = build_dataset_rows(
            [frame(0, 1_000_000_000)],
            [packet(7, 1_010_000_000, parse_error="bad packet")],
        )

        self.assertEqual(rows[0]["is_valid"], 0)
        self.assertEqual(rows[0]["packet_id"], "7")
        self.assertEqual(rows[0]["Speed"], "")
        self.assertEqual(rows[0]["Steer"], "")

    def test_no_packet_outputs_blank_packet_fields(self) -> None:
        rows = build_dataset_rows([frame(0, 1_000_000_000)], [])

        self.assertEqual(rows[0]["is_valid"], 0)
        self.assertEqual(rows[0]["packet_id"], "")
        self.assertEqual(rows[0]["packet_dt_ms"], "")
        self.assertEqual(rows[0]["Speed"], "")


if __name__ == "__main__":
    unittest.main()
