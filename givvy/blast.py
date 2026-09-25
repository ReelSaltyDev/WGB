"""The Time Bomb TCG theme's win celebration: a bomb drops from the top of the window, lands in the middle, burns a
3-second fuse you can watch, and explodes while the window shakes.

The look follows timebombtcg.com (cart.js / styles.css, read 2026-09-24): a gold bomb with a navy cap, a gold fuse and
a spark; the blast is a white-to-gold flash, a gold ring and a blue ring spreading out, and gold shards flung outward;
the recoil shake decays so it settles instead of stopping dead. Like the site, it is skipped when the person has asked
Windows for fewer animations (Settings > Accessibility > Visual effects > Animation effects).

It is drawn on a borderless window laid over the app's own, whose background colour is made see-through: the app
stays usable underneath, and clicks pass through everywhere except the bomb. A Tk canvas cannot blend, and its
stipple patterns do nothing on Windows (tried 2026-09-24: the flash stayed a solid disc), so things fade by shrinking,
thinning and cooling in colour instead. The shake moves the app's window itself and always puts it back where it was."""
from __future__ import annotations

import ctypes
import logging
import math
import random
import time
import tkinter as tk

log = logging.getLogger(__name__)

KEY = "#010203"                 # the see-through colour
GOLD, GOLD_2, GOLD_DARK = "#e5b544", "#f7d98a", "#b8861f"
NAVY, BLUE, WHITE = "#0c1530", "#3f7fe0", "#ffffff"
EMBER = ("#ffffff", "#fff3c4", "#f7d98a", "#ffb347", "#ff8a3c")
ASH = "#5a2a0a"                 # what a spark cools to

DROP_S, LAND_S, FUSE_S, BLAST_S, HOLD_S = 0.7, 0.18, 3.0, 1.3, 1.5    # HOLD_S: the caption stays after the blast
SHAKE_S = 0.6
# the site's keyframes (tb-shake), doubled: a bigger blast than a cart button
SHAKE = ((0, 0, 0), (.08, -18, 10), (.17, 16, -12), (.26, -14, -8), (.35, 12, 10), (.44, -10, 6), (.53, 8, -8),
         (.62, -6, 4), (.71, 6, 4), (.80, -4, -2), (.90, 2, 2), (1.0, 0, 0))
FRAME_MS = 16


def caption_for(account: str, prize: str) -> str:
    """'jgoblin22 Won Free Pack Givey #17'. The bot adds the giveaway's description in [brackets]; not here."""
    import re
    prize = re.sub(r"\s*\[.*\]\s*$", "", (prize or "")).strip()
    if len(prize) > 80:
        prize = prize[:79].rstrip() + "\u2026"
    return f"{account} Won {prize}" if prize else f"{account} Won a Givvy!"


def animations_on() -> bool:
    """Windows' 'Animation effects' switch (SPI_GETCLIENTAREAANIMATION). True where it cannot be asked."""
    try:
        on = ctypes.c_bool(True)
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0):
            return bool(on.value)
    except (AttributeError, OSError):
        pass
    return True


def shake_offset(t: float) -> tuple[float, float]:
    """(dx, dy) at t seconds into the shake: straight lines between the keyframes, 0 outside."""
    f = t / SHAKE_S
    if f <= 0 or f >= 1:
        return 0.0, 0.0
    for (f0, x0, y0), (f1, x1, y1) in zip(SHAKE, SHAKE[1:]):
        if f0 <= f <= f1:
            k = (f - f0) / (f1 - f0)
            return x0 + (x1 - x0) * k, y0 + (y1 - y0) * k
    return 0.0, 0.0


def cool(colour: str, p: float) -> str:
    """A hot colour cooling down: 0 = as it is, 1 = dark ember."""
    return _mix(colour, ASH, max(0.0, min(1.0, p)))


def _mix(a: str, b: str, k: float) -> str:
    a, b = a.lstrip("#"), b.lstrip("#")
    c = [round(int(a[i:i + 2], 16) + (int(b[i:i + 2], 16) - int(a[i:i + 2], 16)) * k) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(c)


def _bezier(p0, p1, p2, p3, n=36):
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


class WindowShaker:
    """Moves the app's real window by (dx, dy) from where it was, maximised or not, and puts it back."""

    def __init__(self, root: tk.Misc):
        self.hwnd = self.home = None
        try:
            import ctypes.wintypes as w
            root.update_idletasks()
            self.user32 = ctypes.windll.user32
            self.hwnd = self.user32.GetParent(root.winfo_id()) or root.winfo_id()
            r = w.RECT()
            if self.user32.GetWindowRect(self.hwnd, ctypes.byref(r)):
                self.home = (r.left, r.top)
        except (AttributeError, OSError, ImportError, tk.TclError):
            pass

    def move(self, dx: float, dy: float):
        if self.home is None:
            return
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
        self.user32.SetWindowPos(self.hwnd, 0, int(self.home[0] + dx), int(self.home[1] + dy), 0, 0,
                                 SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def restore(self):
        self.move(0, 0)


class Blast:
    """One celebration. start() returns False when it did not run (animations off, window hidden, already one on)."""

    running = False

    def __init__(self, root: tk.Misc, caption: str = "", clock=time.monotonic, shaker=None, rng=None):
        self.root, self.clock = root, clock
        self.caption = caption
        self.shaker = shaker
        self.rng = rng or random.Random()
        self.ov = self.cv = None
        self.t0 = 0.0
        self.embers: list[list[float]] = []
        self.shards: list[dict] = []
        self.done = False
        self.job = None

    # ---------------------------------------------------------------- set-up
    def start(self, check_settings: bool = True) -> bool:
        if Blast.running or (check_settings and not animations_on()):
            return False
        try:
            if self.root.state() in ("iconic", "withdrawn") or not self.root.winfo_viewable():
                return False
            self.root.update_idletasks()
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            self.w, self.h = max(200, self.root.winfo_width()), max(200, self.root.winfo_height())
            ov = tk.Toplevel(self.root)
            ov.withdraw()
            ov.overrideredirect(True)
            ov.transient(self.root)                     # stays above the app, not above every other program
            ov.configure(background=KEY)
            try:
                ov.attributes("-transparentcolor", KEY)
            except tk.TclError:                          # not Windows: no see-through, so no celebration
                ov.destroy()
                return False
            ov.geometry(f"{self.w}x{self.h}+{x}+{y}")
            cv = tk.Canvas(ov, background=KEY, highlightthickness=0, width=self.w, height=self.h)
            cv.pack(fill="both", expand=True)
            ov.deiconify()
            ov.lift(self.root)
        except tk.TclError:
            log.debug("no celebration", exc_info=True)
            return False
        self.ov, self.cv = ov, cv
        self.cx, self.cy = self.w / 2, self.h / 2
        self.R = max(40.0, min(90.0, min(self.w, self.h) * 0.075))      # the bomb's radius
        if self.shaker is None:
            self.shaker = WindowShaker(self.root)
        Blast.running = True
        self.t0 = self.clock()
        self._schedule()
        return True

    def _schedule(self):
        self.job = self.ov.after(FRAME_MS, self.frame)

    # ---------------------------------------------------------------- the timeline
    def frame(self):
        if self.done:
            return
        try:
            t = self.clock() - self.t0
            cv = self.cv
            cv.delete("all")
            land = DROP_S + LAND_S
            fuse_end = land + FUSE_S
            if t < DROP_S:                                            # falling, faster and faster
                k = t / DROP_S
                y = -self.R * 2.5 + (self.cy + self.R * 2.5) * k * k
                self.draw_bomb(self.cx, y, 1.0, 1.0, fuse=1.0, t=t)
            elif t < land:                                            # lands: squashes, springs back
                k = (t - DROP_S) / LAND_S
                squash = 1 - 0.22 * math.sin(math.pi * k)
                self.draw_bomb(self.cx, self.cy + self.R * (1 - squash) * 0.6, 1 + (1 - squash) * 0.6, squash,
                               fuse=1.0, t=t)
            elif t < fuse_end:                                        # the fuse burns down
                left = 1 - (t - land) / FUSE_S
                wobble = 0.03 * math.sin(t * 40) * (1 - left)          # the last second, it trembles
                self.draw_bomb(self.cx + wobble * self.R, self.cy, 1.0, 1.0, fuse=left, t=t)
            elif t < fuse_end + BLAST_S:                              # boom
                if not self.shards:
                    self.detonate()
                self.draw_blast(t - fuse_end)
                self.shaker.move(*shake_offset(t - fuse_end))
            elif t < fuse_end + BLAST_S + (HOLD_S if self.caption else 0):
                self.shaker.restore()
            else:
                self.finish()
                return
            if t >= DROP_S:                                           # '<account> Won <prize>', from the landing on
                self.draw_caption()
        except tk.TclError:
            self.finish()
            return
        except Exception:
            log.exception("celebration")
            self.finish()
            return
        self._schedule()

    def finish(self):
        if self.done:
            return
        self.done = True
        Blast.running = False
        try:
            if self.shaker is not None:
                self.shaker.restore()
        except Exception:
            log.exception("putting the window back")
        try:
            if self.ov is not None:
                self.ov.destroy()
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- the bomb (the site's SVG, scaled)
    def draw_bomb(self, x, y, sx, sy, fuse, t):
        cv, R = self.cv, self.R
        k = R / 18.0                                                  # the SVG's body radius is 18

        def P(px, py):                                                # SVG point -> screen, body centre at (x, y)
            return x + (px - 26) * k * sx, y + (py - 42) * k * sy
        # shadow on the "floor" once it is near the middle
        if y > self.cy - R * 3:
            near = max(0.0, 1 - abs(self.cy - y) / (R * 3))
            cv.create_oval(x - R * 1.1 * near, self.cy + R * 0.95, x + R * 1.1 * near, self.cy + R * 1.2,
                           fill="#03060f", outline="")
        # fuse, from the cap to where it is burning
        path = _bezier(P(43, 18), P(52, 12), P(46, 7), P(56, 4))
        n = max(1, int(round(len(path) * fuse)))
        shown = path[:n]
        if len(shown) > 1:
            cv.create_line(*[c for p in shown for c in p], fill=GOLD_DARK, width=max(3, k * 3.6), capstyle="round",
                           smooth=True)
            cv.create_line(*[c for p in shown for c in p], fill=GOLD, width=max(2, k * 2.4), capstyle="round",
                           smooth=True)
        # cap: the navy block with a gold edge, turned 42 degrees
        cxp, cyp = 39.5, 24
        a = math.radians(42)
        corners = []
        for px, py in ((34, 19), (45, 19), (45, 29), (34, 29)):
            dx, dy = px - cxp, py - cyp
            corners.append(P(cxp + dx * math.cos(a) - dy * math.sin(a), cyp + dx * math.sin(a) + dy * math.cos(a)))
        cv.create_polygon(*[c for p in corners for c in p], fill=NAVY, outline=GOLD, width=max(2, k * 2))
        # body: the site's radial gradient, gold-2 at 35%/30% to dark gold at the edge
        bx, by = P(26, 42)
        rx, ry = R * sx, R * sy
        cv.create_oval(bx - rx - 2, by - ry - 2, bx + rx + 2, by + ry + 2, fill="#7a5a12", outline="")
        steps = 16
        hx, hy = bx - rx * 0.30, by - ry * 0.40                          # where the light sits
        for i in range(steps):
            f = i / steps
            r_x, r_y = rx * (1 - f), ry * (1 - f)
            ox, oy = bx + (hx - bx) * f, by + (hy - by) * f
            cv.create_oval(ox - r_x, oy - r_y, ox + r_x, oy + r_y, fill=_mix(GOLD_DARK, GOLD_2, f), outline="")
        hx2, hy2 = P(19, 35)
        cv.create_oval(hx2 - 5 * k, hy2 - 5 * k, hx2 + 5 * k, hy2 + 5 * k, fill="#fff3c4", outline="")
        # the spark where the fuse is burning, and embers thrown off it
        tip = shown[-1]
        flick = 0.75 + 0.5 * self.rng.random()
        rs = k * 5.5 * flick
        cv.create_oval(tip[0] - rs * 1.6, tip[1] - rs * 1.6, tip[0] + rs * 1.6, tip[1] + rs * 1.6, fill="#ffb347",
                       outline="")
        star = []
        spikes = 7
        rot = t * 9
        for i in range(spikes * 2):
            rr = rs * (1.9 if i % 2 == 0 else 0.7) * (0.8 + 0.4 * self.rng.random())
            ang = rot + math.pi * i / spikes
            star += [tip[0] + rr * math.cos(ang), tip[1] + rr * math.sin(ang)]
        cv.create_polygon(*star, fill=self.rng.choice(EMBER[1:4]), outline="")
        cv.create_oval(tip[0] - rs * 0.55, tip[1] - rs * 0.55, tip[0] + rs * 0.55, tip[1] + rs * 0.55, fill=WHITE,
                       outline="")
        if fuse < 1.0 or t > DROP_S:
            for _ in range(3):
                ang = self.rng.uniform(-math.pi, 0)
                sp = self.rng.uniform(1.5, 4.5)
                self.embers.append([tip[0], tip[1], math.cos(ang) * sp, math.sin(ang) * sp, 0, self.rng.choice(EMBER)])
        alive = []
        for e in self.embers:
            e[0] += e[2]; e[1] += e[3]; e[3] += 0.22; e[4] += 1
            if e[4] < 22:
                s = 2.2 * (1 - e[4] / 22) + 0.6
                cv.create_oval(e[0] - s, e[1] - s, e[0] + s, e[1] + s, fill=cool(e[5], e[4] / 22), outline="")
                alive.append(e)
        self.embers = alive

    def draw_caption(self):
        """Under the bomb, big and gold, with a navy outline so it reads over whatever is behind it."""
        if not self.caption:
            return
        cv = self.cv
        size = int(max(18, min(34, self.w / 38)))
        y = self.cy + self.R * 1.75
        opts = dict(text=self.caption, font=("Georgia", size, "bold"), anchor="n", justify="center",
                    width=int(self.w * 0.8))
        d = max(2, size // 12)
        for ox, oy in ((-d, 0), (d, 0), (0, -d), (0, d), (-d, -d), (d, d), (-d, d), (d, -d)):
            cv.create_text(self.cx + ox, y + oy, fill=NAVY, **opts)
        cv.create_text(self.cx, y, fill=GOLD_2, **opts)

    # ---------------------------------------------------------------- the blast (the site's .boom, bigger)
    def detonate(self):
        self.embers = []
        big = min(self.w, self.h)
        n = 40
        for i in range(n):
            a = 2 * math.pi * i / n + self.rng.uniform(-0.18, 0.18)
            d = self.rng.uniform(0.18, 0.5) * big
            self.shards.append(dict(dx=math.cos(a) * d, dy=math.sin(a) * d, rot=self.rng.uniform(-2 * math.pi, 2 * math.pi),
                                    life=self.rng.uniform(0.6, 1.0), size=self.rng.uniform(8, 16)))

    def draw_blast(self, t: float):
        cv, x, y = self.cv, self.cx, self.cy
        big = min(self.w, self.h)
        # flash (tb-flash): a white-hot core in gold bursts out, then the fire collapses and cools
        p = t / 0.6
        if p < 1:
            burst = min(1.0, p / 0.3)
            grow = 1 - (1 - burst) ** 2
            shrink = 1.0 if p < 0.3 else 1 - ((p - 0.3) / 0.7) ** 1.6
            r = (big * 0.05 + big * 0.28 * grow) * shrink
            heat = 0 if p < 0.3 else (p - 0.3) / 0.7
            for scale, colour in ((1.0, GOLD), (0.74, GOLD_2), (0.5, "#fff3c4"), (0.28, WHITE)):
                rr = r * scale * (1 - heat * (1 - scale) * 0.8)                # the white core goes first
                if rr > 1:
                    cv.create_oval(x - rr, y - rr, x + rr, y + rr, fill=cool(colour, heat * 0.85), outline="")
        # rings: gold (tb-ring) then blue (tb-ring2), spreading out
        for delay, dur, reach, colour, width in ((0.0, 0.62, 0.62, GOLD, 7), (0.05, 0.7, 0.5, BLUE, 5)):
            q = (t - delay) / dur
            if 0 <= q < 1:
                rr = big * 0.03 + big * reach * (1 - (1 - q) ** 3)
                cv.create_oval(x - rr, y - rr, x + rr, y + rr, outline=_mix(colour, NAVY, q * 0.8),
                               width=max(1, width * (1 - q)))
        # shards (tb-shard): flung outward, spinning, fading
        for s in self.shards:
            q = t / s["life"]
            if q >= 1:
                continue
            e = 1 - (1 - q) ** 3                                           # quick start, long glide
            sx, sy = x + s["dx"] * e, y + s["dy"] * e
            half = s["size"] / 2 * (1.0 if q < 0.55 else 1 - (q - 0.55) / 0.45)     # burns away at the end
            ang = s["rot"] * e
            pts = []
            for cx_, cy_ in ((-half, -half), (half, -half), (half, half), (-half, half)):
                pts += [sx + cx_ * math.cos(ang) - cy_ * math.sin(ang), sy + cx_ * math.sin(ang) + cy_ * math.cos(ang)]
            cv.create_polygon(*pts, fill=cool(GOLD_2, q * 0.6), outline=GOLD, width=1)
