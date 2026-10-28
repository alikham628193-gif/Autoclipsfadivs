"""Lightweight local tests; run with: python -m unittest test_project.py"""
import unittest
from config import Crop
from editor import _bitrate_number, _crop_expr


class CropTests(unittest.TestCase):
    def test_valid_crop(self):
        Crop(0, 0, 50, 40).validate("CAM")

    def test_crop_overflow_rejected(self):
        with self.assertRaises(ValueError):
            Crop(80, 0, 30, 50).validate("CAM")

    def test_zero_size_rejected(self):
        with self.assertRaises(ValueError):
            Crop(0, 0, 0, 50).validate("GAME")

    def test_crop_expression_contains_rounding(self):
        expr = _crop_expr(Crop(10, 20, 50, 40))
        self.assertIn("trunc(iw*0.500000/2)*2", expr)
        self.assertIn("trunc(ih*0.400000/2)*2", expr)

    def test_bitrate_parser(self):
        self.assertEqual(_bitrate_number("1800k"), 1_800_000)
        self.assertEqual(_bitrate_number("2M"), 2_000_000)
        self.assertEqual(_bitrate_number("1200000"), 1_200_000)


if __name__ == "__main__":
    unittest.main()
