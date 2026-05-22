import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from capture_dataset import OUTPUT_HEIGHT, OUTPUT_WIDTH, resize_frame_to_output, write_jpeg_image


class ImageOutputTests(unittest.TestCase):
    def test_resize_frame_to_output_size(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)

        resized = resize_frame_to_output(frame)

        self.assertEqual(resized.shape, (OUTPUT_HEIGHT, OUTPUT_WIDTH, 3))

    def test_write_jpeg_image_size(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[:, :, 1] = 128

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "frame.jpg"
            write_jpeg_image(frame, path, quality=85)
            loaded = cv2.imread(str(path))

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.shape[:2], (OUTPUT_HEIGHT, OUTPUT_WIDTH))


if __name__ == "__main__":
    unittest.main()
