"""Is the giveaway badge showing a gift (not entered) or a tick (entered)?

The accessibility tree cannot say: the icon's description is 'Giveaway' either way (267 reads out of 267). The
pixels can, and cleanly. Measured on real crops from two emulators against a dark and a bright-blue stream, as the
share of the icon's white pixels falling in each corner of the crop:

              white    top-left  top-right  bottom-left  bottom-right
    tick      0.06     0.00      0.26       0.36         0.05         a thin stroke, low-left to high-right
    gift      0.16     0.19      0.21       0.14-0.15    0.17         a box with a bow: white everywhere
    neither   0-0.13   0.00      0.00       0.45-0.60    0.30-0.47    the card re-opened under the screenshot

'Neither' matters as much as the other two: the screenshot is taken a moment after the read that located the icon,
and the layout can have changed in between. Then the answer is None, and the caller does nothing.
"""
from __future__ import annotations

ENTERED, NOT_ENTERED = "entered", "not_entered"
MARGIN = 8                     # the fixtures were cropped with this much around the icon's bounds
WHITE = 200                    # every channel above this = a white pixel


def _features(img):
    im = img.convert("RGB")
    w, h = im.size
    px = im.load()
    rows = [[min(px[x, y]) > WHITE for x in range(w)] for y in range(h)]
    total = sum(map(sum, rows))
    if not total:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    def share(x0, x1, y0, y1):
        return sum(rows[y][x] for y in range(int(h * y0), int(h * y1)) for x in range(int(w * x0), int(w * x1))) / total
    return total / (w * h), share(0, .45, 0, .45), share(.55, 1, 0, .45), share(0, .45, .55, 1), share(.55, 1, .55, 1)


def icon_state(img) -> str | None:
    """ENTERED, NOT_ENTERED, or None when the crop is neither icon. `img` is the icon's bounds plus MARGIN."""
    white, tl, tr, bl, br = _features(img)
    if 0.03 <= white <= 0.10 and tl <= 0.03 and tr >= 0.15 and bl >= 0.20 and br <= 0.12:
        return ENTERED
    if 0.11 <= white <= 0.24 and min(tl, tr, bl, br) >= 0.08:
        return NOT_ENTERED
    return None


def crop_box(bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return (bounds[0] - MARGIN, bounds[1] - MARGIN, bounds[2] + MARGIN, bounds[3] + MARGIN)
