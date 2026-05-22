"""Record a minimal Forza image plus UDP dataset.

The collector writes one 420x240 JPEG per captured frame and one matching row
in dataset.csv after capture stops. It intentionally does not record audio,
AVI files, clips, or model-ready archives.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import logging
import queue
import socket
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from packet_format import PacketSizeError, parse_packet


OUTPUT_WIDTH = 420
OUTPUT_HEIGHT = 240
OUTPUT_FPS = 60
DEFAULT_JPEG_QUALITY = 85
DEFAULT_OUTPUT_DIR = "data/sessions"
DEFAULT_UDP_HOST = "0.0.0.0"
DEFAULT_UDP_PORT = 9999
DEFAULT_MAX_PACKET_GAP_MS = 25.0
DEFAULT_SOCKET_TIMEOUT_S = 0.2
DEFAULT_FLUSH_EVERY = 120
DEFAULT_VIDEO_QUEUE_SIZE = 180

TELEMETRY_COLUMNS = (
    "TimestampMS",
    "Speed",
    "CurrentEngineRpm",
    "Gear",
    "Accel",
    "Brake",
    "Steer",
    "IsRaceOn",
    "PositionX",
    "PositionY",
    "PositionZ",
    "AccelerationX",
    "AccelerationY",
    "AccelerationZ",
    "VelocityX",
    "VelocityY",
    "VelocityZ",
    "AngularVelocityX",
    "AngularVelocityY",
    "AngularVelocityZ",
    "Yaw",
    "Pitch",
    "Roll",
    "NormalizedSuspensionTravelFrontLeft",
    "NormalizedSuspensionTravelFrontRight",
    "NormalizedSuspensionTravelRearLeft",
    "NormalizedSuspensionTravelRearRight",
    "TireSlipRatioFrontLeft",
    "TireSlipRatioFrontRight",
    "TireSlipRatioRearLeft",
    "TireSlipRatioRearRight",
    "WheelRotationSpeedFrontLeft",
    "WheelRotationSpeedFrontRight",
    "WheelRotationSpeedRearLeft",
    "WheelRotationSpeedRearRight",
    "Power",
    "Torque",
)

FRAME_FIELDS = (
    "frame_id",
    "image_path",
    "t_present_perf_ns",
    "t_grab_perf_ns",
    "source_width",
    "source_height",
    "width",
    "height",
    "queue_dropped_before",
)
PACKET_FIELDS = ("packet_id", "t_recv_perf_ns", "packet_size", "parse_error", *TELEMETRY_COLUMNS)
DATASET_FIELDS = (
    "frame_id",
    "image_path",
    "t_present_perf_ns",
    "packet_id",
    "t_recv_perf_ns",
    "packet_dt_ms",
    "is_valid",
    *TELEMETRY_COLUMNS,
)


@dataclass(frozen=True)
class CaptureConfig:
    """Small set of settings for this one-purpose collector."""

    output_dir: str = DEFAULT_OUTPUT_DIR
    udp_host: str = DEFAULT_UDP_HOST
    udp_port: int = DEFAULT_UDP_PORT
    fps: int = OUTPUT_FPS
    image_width: int = OUTPUT_WIDTH
    image_height: int = OUTPUT_HEIGHT
    jpeg_quality: int = DEFAULT_JPEG_QUALITY
    max_packet_gap_ms: float = DEFAULT_MAX_PACKET_GAP_MS
    socket_timeout_s: float = DEFAULT_SOCKET_TIMEOUT_S
    flush_every: int = DEFAULT_FLUSH_EVERY
    video_queue_size: int = DEFAULT_VIDEO_QUEUE_SIZE


def perf_ns() -> int:
    """Return the collector's canonical monotonic timestamp."""

    return time.perf_counter_ns()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def config_dict(config: CaptureConfig) -> dict[str, Any]:
    return asdict(config)


def make_session_dir(output_dir: str) -> Path:
    session_dir = Path(output_dir) / datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir.mkdir(parents=True, exist_ok=False)
    return session_dir


def setup_logger(session_dir: Path) -> logging.Logger:
    logger = logging.getLogger("forza_dataset_capture")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(session_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def write_manifest(session_dir: Path, manifest: dict[str, Any]) -> None:
    with (session_dir / "manifest.json").open("w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)
        file.write("\n")


def resize_frame_to_output(frame: Any, width: int = OUTPUT_WIDTH, height: int = OUTPUT_HEIGHT) -> Any:
    """Resize a BGR frame to the fixed dataset image size."""

    import cv2

    source_height, source_width = frame.shape[:2]
    interpolation = cv2.INTER_AREA if source_width >= width and source_height >= height else cv2.INTER_LINEAR
    return cv2.resize(frame, (width, height), interpolation=interpolation)


def write_jpeg_image(frame: Any, path: Path, quality: int = DEFAULT_JPEG_QUALITY) -> None:
    """Resize and write one BGR frame as a JPEG image."""

    import cv2

    image = resize_frame_to_output(frame)
    ok = cv2.imwrite(str(path), image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError(f"could not write image: {path}")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def nearest_packet(packet_times: list[int], frame_time: int) -> int | None:
    """Return the index of the packet timestamp closest to a frame time."""

    if not packet_times:
        return None
    index = bisect.bisect_left(packet_times, frame_time)
    candidates = []
    if index > 0:
        candidates.append(index - 1)
    if index < len(packet_times):
        candidates.append(index)
    return min(candidates, key=lambda item: abs(packet_times[item] - frame_time))


def build_dataset_rows(
    frames: list[dict[str, str]],
    packets: list[dict[str, str]],
    max_packet_gap_ms: float = DEFAULT_MAX_PACKET_GAP_MS,
) -> list[dict[str, Any]]:
    """Create one dataset row per frame by attaching the nearest UDP packet."""

    packet_rows = [row for row in packets if row.get("t_recv_perf_ns")]
    packet_times = [int(row["t_recv_perf_ns"]) for row in packet_rows]
    dataset_rows: list[dict[str, Any]] = []

    for frame in frames:
        frame_time = int(frame["t_present_perf_ns"])
        packet_index = nearest_packet(packet_times, frame_time)
        packet_row = packet_rows[packet_index] if packet_index is not None else {}

        packet_dt_ms = ""
        packet_ok = False
        parse_ok = False
        if packet_index is not None:
            packet_dt_ms_float = (int(packet_row["t_recv_perf_ns"]) - frame_time) / 1_000_000
            packet_dt_ms = f"{packet_dt_ms_float:.3f}"
            parse_ok = not packet_row.get("parse_error")
            packet_ok = abs(packet_dt_ms_float) <= max_packet_gap_ms and parse_ok

        row: dict[str, Any] = {
            "frame_id": frame["frame_id"],
            "image_path": frame["image_path"],
            "t_present_perf_ns": frame["t_present_perf_ns"],
            "packet_id": packet_row.get("packet_id", ""),
            "t_recv_perf_ns": packet_row.get("t_recv_perf_ns", ""),
            "packet_dt_ms": packet_dt_ms,
            "is_valid": int(packet_ok),
        }
        for column in TELEMETRY_COLUMNS:
            row[column] = packet_row.get(column, "") if parse_ok else ""
        dataset_rows.append(row)

    return dataset_rows


def write_dataset_csv(session_dir: Path, max_packet_gap_ms: float) -> dict[str, int]:
    frames = read_csv_rows(session_dir / "frames.csv")
    packets = read_csv_rows(session_dir / "packets.csv")
    rows = build_dataset_rows(frames, packets, max_packet_gap_ms=max_packet_gap_ms)
    valid_rows = 0

    with (session_dir / "dataset.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=DATASET_FIELDS)
        writer.writeheader()
        for row in rows:
            if str(row["is_valid"]) == "1":
                valid_rows += 1
            writer.writerow(row)

    return {"rows": len(rows), "valid_rows": valid_rows}


def video_worker(
    session_dir: Path,
    config: CaptureConfig,
    stop_event: threading.Event,
    logger: logging.Logger,
    summary: dict[str, Any],
) -> None:
    """Capture frames with DXcam and write resized JPEG images plus frames.csv."""

    try:
        import dxcam
    except ImportError as exc:
        raise RuntimeError("video capture needs dxcam installed in the forza conda env") from exc

    images_dir = session_dir / "images"
    images_dir.mkdir()
    frame_queue: queue.Queue[tuple[int, int, int, int, int, int, Any] | None] = queue.Queue(
        maxsize=config.video_queue_size
    )
    captured = 0
    queued = 0
    dropped = 0
    written = 0
    grabber_errors: list[str] = []

    def grabber() -> None:
        nonlocal captured, queued, dropped
        camera = None
        try:
            camera = dxcam.create(output_color="BGR")
            camera.start(target_fps=config.fps, video_mode=True)
            logger.info("Video capture started at target_fps=%d", config.fps)
            while not stop_event.is_set():
                frame = camera.get_latest_frame()
                if frame is None:
                    time.sleep(0.001)
                    continue

                t_grab_ns = perf_ns()
                source_height, source_width = frame.shape[:2]
                captured += 1
                item = (queued, t_grab_ns, t_grab_ns, source_width, source_height, dropped, frame)
                try:
                    frame_queue.put_nowait(item)
                    queued += 1
                except queue.Full:
                    dropped += 1
        except Exception as exc:  # noqa: BLE001 - surfaced by parent thread.
            grabber_errors.append(str(exc))
            logger.exception("Video grabber failed")
            stop_event.set()
        finally:
            if camera is not None:
                camera.stop()
            while True:
                try:
                    frame_queue.put_nowait(None)
                    break
                except queue.Full:
                    try:
                        frame_queue.get_nowait()
                    except queue.Empty:
                        pass
            logger.info("Video grabber stopped")

    grab_thread = threading.Thread(target=grabber, name="video-grabber", daemon=True)
    grab_thread.start()

    frames_path = session_dir / "frames.csv"
    try:
        with frames_path.open("w", newline="", encoding="utf-8") as frames_file:
            writer = csv.DictWriter(frames_file, fieldnames=FRAME_FIELDS)
            writer.writeheader()

            while True:
                try:
                    item = frame_queue.get(timeout=0.2)
                except queue.Empty:
                    if stop_event.is_set() and not grab_thread.is_alive():
                        break
                    continue
                if item is None:
                    break

                frame_id, t_present_ns, t_grab_ns, source_width, source_height, dropped_before, frame = item
                image_rel_path = f"images/{frame_id:06d}.jpg"
                write_jpeg_image(frame, session_dir / image_rel_path, config.jpeg_quality)
                writer.writerow(
                    {
                        "frame_id": frame_id,
                        "image_path": image_rel_path,
                        "t_present_perf_ns": t_present_ns,
                        "t_grab_perf_ns": t_grab_ns,
                        "source_width": source_width,
                        "source_height": source_height,
                        "width": config.image_width,
                        "height": config.image_height,
                        "queue_dropped_before": dropped_before,
                    }
                )
                written += 1
                if written % config.flush_every == 0:
                    frames_file.flush()
    finally:
        stop_event.set()
        grab_thread.join(timeout=3)
        summary["video"] = {"captured": captured, "queued": queued, "written": written, "dropped": dropped}
        logger.info("Video done: captured=%d queued=%d written=%d dropped=%d", captured, queued, written, dropped)
        if grabber_errors:
            raise RuntimeError(f"video grabber failed: {grabber_errors[0]}")


def udp_worker(
    session_dir: Path,
    config: CaptureConfig,
    stop_event: threading.Event,
    logger: logging.Logger,
    summary: dict[str, Any],
) -> None:
    """Listen for Forza UDP packets and write parsed packet rows."""

    packet_count = 0
    parse_errors = 0

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        udp_socket.bind((config.udp_host, config.udp_port))
        udp_socket.settimeout(config.socket_timeout_s)
        logger.info("UDP listener started on %s:%d", config.udp_host, config.udp_port)

        with (session_dir / "packets.csv").open("w", newline="", encoding="utf-8") as packets_file:
            writer = csv.DictWriter(packets_file, fieldnames=PACKET_FIELDS)
            writer.writeheader()

            while not stop_event.is_set():
                try:
                    payload, _address = udp_socket.recvfrom(2048)
                except socket.timeout:
                    continue

                t_recv_ns = perf_ns()
                row: dict[str, Any] = {
                    "packet_id": packet_count,
                    "t_recv_perf_ns": t_recv_ns,
                    "packet_size": len(payload),
                    "parse_error": "",
                }
                try:
                    telemetry = parse_packet(payload, packet_format="horizon")
                except (PacketSizeError, ValueError) as exc:
                    telemetry = {}
                    parse_errors += 1
                    row["parse_error"] = str(exc)

                for column in TELEMETRY_COLUMNS:
                    row[column] = telemetry.get(column, "")
                writer.writerow(row)

                packet_count += 1
                if packet_count % config.flush_every == 0:
                    packets_file.flush()

    summary["udp"] = {"packets": packet_count, "parse_errors": parse_errors}
    logger.info("UDP done: packets=%d parse_errors=%d", packet_count, parse_errors)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record 420x240 Forza frames plus UDP telemetry")
    parser.add_argument("--duration", type=float, default=None, help="seconds to record; omit to stop with Ctrl+C")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--host", default=DEFAULT_UDP_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_UDP_PORT)
    parser.add_argument("--jpeg-quality", type=int, default=DEFAULT_JPEG_QUALITY)
    parser.add_argument("--max-packet-gap-ms", type=float, default=DEFAULT_MAX_PACKET_GAP_MS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = CaptureConfig(
        output_dir=args.output_dir,
        udp_host=args.host,
        udp_port=args.port,
        jpeg_quality=args.jpeg_quality,
        max_packet_gap_ms=args.max_packet_gap_ms,
    )
    session_dir = make_session_dir(config.output_dir)
    logger = setup_logger(session_dir)
    stop_event = threading.Event()
    summary: dict[str, Any] = {}
    worker_errors: list[str] = []
    manifest = {
        "session_dir": str(session_dir),
        "started_wall_time_iso": now_iso(),
        "perf_start_ns": perf_ns(),
        "config": config_dict(config),
        "enabled": {"video": True, "udp": True, "audio": False},
    }
    write_manifest(session_dir, manifest)

    def run_worker(name: str, target: Any) -> None:
        try:
            target(session_dir, config, stop_event, logger, summary)
        except Exception as exc:  # noqa: BLE001 - logged and surfaced after shutdown.
            worker_errors.append(f"{name}: {exc}")
            logger.exception("%s worker failed", name)
            stop_event.set()

    workers = [
        threading.Thread(target=run_worker, args=("video", video_worker), name="video"),
        threading.Thread(target=run_worker, args=("udp", udp_worker), name="udp"),
    ]

    try:
        for worker in workers:
            worker.start()

        if args.duration is None:
            while any(worker.is_alive() for worker in workers):
                if worker_errors:
                    break
                time.sleep(0.2)
        else:
            deadline = time.monotonic() + args.duration
            while time.monotonic() < deadline and not worker_errors:
                time.sleep(0.2)
    except KeyboardInterrupt:
        logger.info("Stop requested")
    finally:
        stop_event.set()
        for worker in workers:
            worker.join()

        if not worker_errors:
            summary["dataset"] = write_dataset_csv(session_dir, config.max_packet_gap_ms)
            logger.info(
                "Dataset done: rows=%d valid_rows=%d",
                summary["dataset"]["rows"],
                summary["dataset"]["valid_rows"],
            )

        manifest["ended_wall_time_iso"] = now_iso()
        manifest["perf_end_ns"] = perf_ns()
        manifest["summary"] = summary
        if worker_errors:
            manifest["errors"] = worker_errors
        write_manifest(session_dir, manifest)
        logger.info("Session written to %s", session_dir)
        print(session_dir)
        if worker_errors:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
