"""The Nunu & Willump theme's win celebration: Nunu Bot rolls across the window from left to right pushing a snowball
that grows as it goes, '<account> Won <prize>' pops up in the middle, and the snowball bursts at the right edge while
the window shakes.

The characters are League of Legends' own Nunu Bot (skin 4), W ('Biggest Snowball Ever!') animation, rendered from the
game's files in Blender into themes/nunu/ on this PC (roll_00..15.png, 16 frames at the game's 24 fps). That art is
Riot's: it is never in the code or on GitHub, and without the folder the theme is simply not offered. The snowball is
drawn here: a snow texture that turns as it rolls under lighting that does not, so it reads as a ball rolling.

It uses the Time Bomb celebration's see-through window, window shake and caption (givvy/blast.py). A see-through
window has no half-transparent pixels, so the sprites' soft edges are cut at half opacity."""
from __future__ import annotations

import logging
import math
import tkinter as tk
from pathlib import Path

from .blast import HOLD_S, Blast, _mix, shake_offset

log = logging.getLogger(__name__)

CROSS_S = 4.8               # left edge to right edge
FPS = 24                    # the game's animation rate
ICE, ICE_2, SNOW, NAVY = "#8fd8ff", "#bfeaff", "#ffffff", "#0a1726"
SNOW_BITS = ("#ffffff", "#f2f9ff", "#dcefff", "#bfe0fb")
POP_S = 0.4
BURST_S = 1.1


def _hard_alpha(img):
    """Cut soft edges at half opacity: a see-through window cannot show half-transparent pixels."""
    a = img.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
    img.putalpha(a)
    return img


class Snowball(Blast):
    """One celebration. start() is Blast's; the timeline is this one's."""

    def __init__(self, root: tk.Misc, caption: str, folder: Path, **kw):
        super().__init__(root, caption, **kw)
        self.folder = Path(folder)
        self.parts: list[list[float]] = []
        self.burst_at = None
        self.angle = 0.0
        self.last_x = None
        self.images = []                                  # PhotoImages must be kept alive while shown

    def start(self, check_settings: bool = True) -> bool:
        try:
            from PIL import Image
            self.src = [Image.open(self.folder / f"roll_{i:02d}.png").convert("RGBA") for i in range(16)]
            self.tex = Image.open(self.folder / "snow_texture.png").convert("RGB")
            self.shade = Image.open(self.folder / "snow_shade.png").convert("RGB")
            self.mask = Image.open(self.folder / "snow_mask.png").convert("L")
            layout = dict(line.split("=") for line in (self.folder / "layout.txt").read_text().split())
            self.hands, self.ground = float(layout["hands"]), float(layout["ground"])
        except (OSError, KeyError, ValueError):
            log.warning("Nunu & Willump art missing or unreadable in %s", self.folder)
            return False
        if not super().start(check_settings):
            return False
        from PIL import ImageTk
        self.sh = max(170.0, min(400.0, self.h * 0.40))                  # the characters' height on screen
        k = self.sh / self.src[0].height
        self.sw = self.src[0].width * k
        self.sprites = [ImageTk.PhotoImage(_hard_alpha(im.resize((max(1, round(self.sw)), round(self.sh)))), master=self.cv)
                        for im in self.src]
        self.floor = self.cy + self.sh * 0.42                             # where the wheels and the ball touch down
        self.r_min, self.r_max = self.sh * 0.16, self.sh * 0.52
        self.x0 = -self.sw - self.r_max * 2.2                             # fully off the left edge
        self.x1 = self.w + 20                                             # fully off the right edge
        return True

    # ---------------------------------------------------------------- the timeline
    def frame(self):
        if self.done:
            return
        try:
            t = self.clock() - self.t0
            self.cv.delete("all")
            self.images = []
            p = min(1.0, t / CROSS_S)
            x = self.x0 + (self.x1 - self.x0) * p                        # the characters' left edge
            r = self.r_min + (self.r_max - self.r_min) * min(1.0, p / 0.8)
            bx = x + self.sw * self.hands + r * 0.8                       # the ball, just past Willump's hands
            by = self.floor - r
            if self.last_x is not None:
                self.angle += (x - self.last_x) / max(1.0, r)              # rolling without slipping
            self.last_x = x
            if self.burst_at is None and bx + r >= self.w - 6:
                self.burst_at = t
                self.burst(bx, by, r)
            self.draw_parts()
            if t < CROSS_S:
                bob = abs(math.sin(t * 9)) * self.sh * 0.02
                img = self.sprites[int(t * FPS) % len(self.sprites)]
                self.cv.create_image(x, self.floor - self.ground * self.sh - bob, image=img, anchor="nw")
                if self.burst_at is None:
                    self.draw_ball(bx, by, r)
                    self.spray(bx, by, r)
            if self.burst_at is not None:
                since = t - self.burst_at
                self.draw_burst(since)
                self.shaker.move(*shake_offset(since))
            end = max(CROSS_S, (self.burst_at or CROSS_S) + BURST_S)
            if t >= end:
                self.shaker.restore()
            if t >= CROSS_S * 0.2:                                        # the caption, once they are on their way
                self.draw_caption(t - CROSS_S * 0.2)
            if t >= end + (HOLD_S if self.caption else 0):
                self.finish()
                return
        except tk.TclError:
            self.finish()
            return
        except Exception:
            log.exception("snowball celebration")
            self.finish()
            return
        self._schedule()

    # ---------------------------------------------------------------- the snowball
    def draw_ball(self, bx, by, r):
        from PIL import Image, ImageChops, ImageTk
        d = max(4, int(r * 2))
        tex = self.tex.resize((d, d)).rotate(-math.degrees(self.angle), resample=Image.BICUBIC, fillcolor=(236, 245, 253))
        ball = ImageChops.multiply(tex, self.shade.resize((d, d))).convert("RGBA")
        ball.putalpha(self.mask.resize((d, d)).point(lambda v: 255 if v >= 128 else 0))
        img = ImageTk.PhotoImage(ball, master=self.cv)
        self.images.append(img)
        self.cv.create_image(bx, by, image=img)

    def spray(self, bx, by, r):
        """Snow kicked up behind and under the ball."""
        for _ in range(3):
            self.parts.append([bx - r * self.rng.uniform(0.2, 0.9), self.floor - self.rng.uniform(0, r * 0.25),
                               self.rng.uniform(-4.5, -1.0), self.rng.uniform(-4.0, -1.2), 0, 26,
                               self.rng.uniform(2, 5.5), self.rng.choice(SNOW_BITS)])

    def draw_parts(self):
        alive = []
        for q in self.parts:
            q[0] += q[2]; q[1] += q[3]; q[3] += 0.25; q[4] += 1
            if q[4] < q[5]:
                s = q[6] * (1 - q[4] / q[5]) + 0.5
                self.cv.create_oval(q[0] - s, q[1] - s, q[0] + s, q[1] + s, fill=q[7], outline="")
                alive.append(q)
        self.parts = alive

    # ---------------------------------------------------------------- the burst at the right edge
    def burst(self, bx, by, r):
        self.bx, self.by, self.br = bx, by, r
        for i in range(70):
            a = self.rng.uniform(math.pi * 0.5, math.pi * 1.5) if i % 3 else self.rng.uniform(0, 2 * math.pi)
            sp = self.rng.uniform(3, 13)
            self.parts.append([bx + math.cos(a) * r * 0.5, by + math.sin(a) * r * 0.5, math.cos(a) * sp,
                               math.sin(a) * sp - 3, 0, self.rng.randint(28, 50), self.rng.uniform(3, 10),
                               self.rng.choice(SNOW_BITS)])

    def draw_burst(self, t):
        q = t / 0.5
        if q < 1:                                                     # a puff of powder, spreading and thinning
            rr = self.br * (0.9 + 1.4 * (1 - (1 - q) ** 2))
            for scale, colour in ((1.0, ICE_2), (0.7, "#e6f6ff"), (0.4, SNOW)):
                s = rr * scale * (1 - q * 0.6 * (1 - scale))
                self.cv.create_oval(self.bx - s, self.by - s, self.bx + s, self.by + s,
                                    fill=_mix(colour, NAVY, q * 0.75), outline="")
        q2 = t / 0.7
        if q2 < 1:                                                    # an icy ring
            rr = self.br * (0.8 + 2.8 * (1 - (1 - q2) ** 3))
            self.cv.create_oval(self.bx - rr, self.by - rr, self.bx + rr, self.by + rr,
                                outline=_mix(ICE, NAVY, q2 * 0.8), width=max(1, 6 * (1 - q2)))

    # ---------------------------------------------------------------- '<account> Won <prize>', popping in
    def draw_caption(self, since: float = 99.0):
        if not self.caption:
            return
        pop = min(1.0, since / POP_S)
        scale = 1 + 0.25 * math.sin(pop * math.pi) if pop < 1 else 1.0         # overshoots, settles
        scale *= 0.35 + 0.65 * min(1.0, pop * 2.5)
        size = max(8, int(max(18, min(34, self.w / 38)) * scale))
        y = self.floor + self.sh * 0.12
        opts = dict(text=self.caption, font=("Segoe UI Black", size), anchor="n", justify="center",
                    width=int(self.w * 0.8))
        d = max(2, size // 12)
        for ox, oy in ((-d, 0), (d, 0), (0, -d), (0, d), (-d, -d), (d, d), (-d, d), (d, -d)):
            self.cv.create_text(self.cx + ox, y + oy, fill=NAVY, **opts)
        self.cv.create_text(self.cx, y, fill="#eaf6ff", **opts)
