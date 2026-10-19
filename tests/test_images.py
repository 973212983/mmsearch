import io
import unittest

import numpy as np
from PIL import Image, ImageDraw

from mmsearch import media


def picture(seed: int, size=(640, 480)) -> Image.Image:
    rng = np.random.default_rng(seed)
    img = Image.new("RGB", size, tuple(int(c) for c in rng.integers(0, 255, 3)))
    d = ImageDraw.Draw(img)
    for _ in range(12):
        x, y = rng.integers(0, size[0]), rng.integers(0, size[1])
        d.ellipse((x, y, x + rng.integers(40, 200), y + rng.integers(40, 200)),
                  fill=tuple(int(c) for c in rng.integers(0, 255, 3)))
    return img


def recompress(img: Image.Image, scale: float, quality: int) -> Image.Image:
    img = img.resize((int(img.width * scale), int(img.height * scale)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return Image.open(io.BytesIO(buf.getvalue()))


def distance(a: int, b: int) -> int:
    return bin((a ^ b) & (2 ** 64 - 1)).count("1")


class DhashTests(unittest.TestCase):
    def test_resized_and_recompressed_copy_stays_close(self):
        img = picture(1)
        self.assertLessEqual(distance(media.dhash(img), media.dhash(recompress(img, 0.25, 50))), 8)

    def test_different_pictures_are_far_apart(self):
        self.assertGreater(distance(media.dhash(picture(1)), media.dhash(picture(2))), 8)

    def test_flat_image_has_no_hash(self):
        self.assertEqual(0, media.dhash(Image.new("RGB", (100, 100), (30, 90, 200))))

    def test_hash_fits_a_signed_64_bit_column(self):
        h = media.dhash(picture(3))
        self.assertTrue(-2 ** 63 <= h < 2 ** 63)


class ToRgbTests(unittest.TestCase):
    def icon(self, color):
        """透明底上画一道横杠；透明处底下的 RGB 和图案同色，直接 convert('RGB') 会变成一整块纯色。"""
        img = Image.new("RGBA", (48, 48), color + (0,))
        ImageDraw.Draw(img).rectangle((8, 20, 40, 28), fill=color + (255,))
        return img

    def test_dark_icon_is_drawn_on_white(self):
        rgb = np.asarray(media.to_rgb(self.icon((0, 0, 0))))
        self.assertEqual((255, 255, 255), tuple(rgb[0, 0]))
        self.assertEqual((0, 0, 0), tuple(rgb[24, 24]))

    def test_light_icon_is_drawn_on_dark_background(self):
        rgb = np.asarray(media.to_rgb(self.icon((250, 250, 250))))
        self.assertLess(rgb[0, 0].max(), 64)
        self.assertEqual((250, 250, 250), tuple(rgb[24, 24]))

    def test_palette_image_with_transparency(self):
        img = self.icon((0, 0, 0)).convert("P")
        img.info["transparency"] = img.getpixel((0, 0))
        self.assertEqual((255, 255, 255), tuple(np.asarray(media.to_rgb(img))[0, 0]))

    def test_opaque_images_are_unchanged(self):
        img = picture(4)
        self.assertTrue(np.array_equal(np.asarray(img), np.asarray(media.to_rgb(img))))
        self.assertEqual("RGB", media.to_rgb(img.convert("L")).mode)


if __name__ == "__main__":
    unittest.main()
