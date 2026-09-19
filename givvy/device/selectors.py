"""Selectors for the Whatnot Android app.

Verified against real dumps from an S24 Ultra (Android 16) on 2026-09-15. The app
is React Native and exposes NO resource-ids, so everything keys off `text`,
`content-desc` and geometry. Keep all UI knowledge here; nothing else should know
Whatnot's strings. Re-record fixtures with tools/dump_ui.ps1 when the app changes.
"""
from __future__ import annotations

import html
import re

WHATNOT_PACKAGE = "com.whatnot_mobile"
LAUNCHERS = {"com.sec.android.app.launcher", "com.google.android.apps.nexuslauncher", "com.android.launcher3"}

_NODE = re.compile(r"<node [^>]*?>")
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

# --- verified against the real dumps ---
GIVEAWAY_WORD = re.compile(r"^giveaway$", re.I)      # badge label AND icon content-desc
ENTRIES_LABEL = re.compile(r"^entr(y|ies)$", re.I)   # the count lives in the PREVIOUS node
NUMERIC = re.compile(r"^(\d[\d,]*)$")
ENTRIES_INLINE = re.compile(r"^(\d[\d,]*)\s+entr(y|ies)$", re.I)   # expanded card
SAME_COLUMN_PX = 40                                  # count and label share a left edge

# --- guesses, to be confirmed by the first supervised dry run: the badge must be
# --- expanded before any of these appear, which was not done unattended ---
# Measured on the real expanded card. The button is "Enter Giveaway" when you
# already follow the seller and "Follow Host & Enter Giveaway" when you do not,
# so this must not be anchored at both ends.
ENTER_BUTTON = re.compile(r"^enter( now)?$|enter giveaway", re.I)
# The combined button follows AND enters in one tap, so no separate Follow tap.
FOLLOW_AND_ENTER = re.compile(r"follow host.*enter giveaway", re.I)
# Seen live: this used to include the phrase 'good luck'. A seller's listing read
# 'BEST ODDS! ... GOOD LUCK!', every read said 'already entered', and emu2 sat in
# the stream never entering. Only the app's own, whole-line confirmation counts,
# and only in the top of the screen where the card is (see is_entered).
ENTERED = re.compile(r"^(you['’]re in the giveaway|you are in the giveaway|you['’]re entered|you are entered)[.!]?$", re.I)
FOLLOW_BUTTON = re.compile(r"^follow$", re.I)
FOLLOW_TO_ENTER = re.compile(r"follow(ers?)? (to|only)|must follow|follow .* to enter", re.I)
# "you won" must not match "You won't be able to go live..." — the account
# verification banner, which otherwise fires a spurious win alert. Measured.
# The real dialog (captured): 'Giveaway Winner' / '<username> won the giveaway!'.
# It used to match 'congrat' and 'winner!' anywhere, i.e. any chat message.
WON = re.compile(r"^you won\b(?!['’]t)", re.I)
WON_BY = re.compile(r"^@?(\S+) won the giveaway", re.I)
CARD_ZONE = 0.35    # the giveaway card and winner dialog live in the top third; chat and listings below
# Positive marker that we are actually inside a livestream rather than the app
# home screen: the stream has a Leave control and a chat composer.
IN_STREAM = re.compile(r"^(leave|say something…?)$", re.I)
LOGIN = re.compile(r"^(log in|sign up|continue with google|join whatnot)", re.I)


def _unescape(s: str) -> str:
    """Every XML/HTML entity, not a hand-picked five. uiautomator writes emoji as
    numeric entities, so a prize came through as '&#128123;PITCH BLACK BOOSTER PACK'
    in the stream list, the log and Discord."""
    return html.unescape(s)


def parse_nodes(xml: str) -> list[dict]:
    """Every <node> as an attribute dict, in document order. Order matters: the
    entry count is the node immediately before the 'Entries' label."""
    return [{k: _unescape(v) for k, v in _ATTR.findall(m.group(0))} for m in _NODE.finditer(xml)]


def text_of(node: dict) -> str:
    return (node.get("text") or node.get("content-desc") or "").strip()


def bounds_of(node: dict) -> tuple[int, int, int, int] | None:
    m = _BOUNDS.match(node.get("bounds", ""))
    return tuple(map(int, m.groups())) if m else None


def center(node: dict) -> tuple[int, int] | None:
    b = bounds_of(node)
    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2) if b else None


def find(nodes: list[dict], pattern: re.Pattern) -> dict | None:
    for n in nodes:
        if pattern.search(text_of(n)):
            return n
    return None


def _badge_nodes(nodes: list[dict]) -> list[dict]:
    return [n for n in nodes if GIVEAWAY_WORD.match(text_of(n))]


def card_visible(nodes: list[dict]) -> bool:
    return bool(_badge_nodes(nodes))


def entries_count(nodes: list[dict]) -> int | None:
    """Read the entry count, in either of the two layouts the app uses.

    Collapsed badge: the count is its own node immediately before a bare
    'Entries' label, sharing its left edge.
    Expanded card:   a single node reads '1,109 Entries'.
    Both were observed on the real phone, so both are handled.
    """
    for n in nodes:                                   # expanded: "1,109 Entries"
        m = ENTRIES_INLINE.match((n.get("text") or "").strip())
        if m:
            return int(m.group(1).replace(",", ""))
    for i, n in enumerate(nodes):
        if not ENTRIES_LABEL.match((n.get("text") or "").strip()):
            continue
        label = bounds_of(n)
        for j in range(i - 1, max(-1, i - 4), -1):
            m = NUMERIC.match((nodes[j].get("text") or "").strip())
            if not m:
                continue
            box = bounds_of(nodes[j])
            if label and box and abs(box[0] - label[0]) > SAME_COLUMN_PX:
                continue                      # a number in some other column
            return int(m.group(1).replace(",", ""))
    return None


CLOSE_SHEET = re.compile(r"^close sheet$", re.I)
# Captured live. Interstitial shown BEFORE the stream loads:
#   "ATTENTION - additional tariffs will apply" / Join Show / Learn More / Don't show this again
# and, once inside the same seller's stream, the listing line reads
#   "CA$18.20 (est. $13.07) Shipping + Taxes + Tariffs"   (domestic: "Shipping + Taxes")
TARIFF_NOTICE = re.compile(r"additional\s+tar+if+s?\b|tar+if+s?\s+will\s+apply|shipping\s*\+\s*taxes\s*\+\s*tar+if+s?\b", re.I)
JOIN_SHOW = re.compile(r"^join show$", re.I)


def tariff_notice(nodes: list[dict]) -> bool:
    """Does this seller ship from another country (tariffs charged on delivery)?"""
    return find(nodes, TARIFF_NOTICE) is not None


def _screen_height(nodes: list[dict]) -> int:
    return max((b[3] for b in (bounds_of(n) for n in nodes) if b), default=0)


def _in_card_zone(n: dict, height: int) -> bool:
    b = bounds_of(n)
    return bool(b) and height > 0 and b[1] < height * CARD_ZONE


def is_entered(nodes: list[dict]) -> bool:
    """The app's own 'You're in the Giveaway', as a whole line, up where the card
    is. Chat and listing text can say anything; it must never count."""
    h = _screen_height(nodes)
    return any(ENTERED.match(text_of(n).strip()) and _in_card_zone(n, h) for n in nodes)


def is_won(nodes: list[dict], usernames: set[str] | frozenset = frozenset()) -> bool:
    """WE won: 'You won ...' or '<our username> won the giveaway!' in the dialog.
    Somebody else's name in the same dialog is not a win."""
    h = _screen_height(nodes)
    names = {u.lower().lstrip("@") for u in usernames if u}
    for n in nodes:
        if not _in_card_zone(n, h):
            continue
        text = text_of(n).strip()
        if WON.match(text):
            return True
        m = WON_BY.match(text)
        if m and m.group(1).lower() in names:
            return True
    return False


def overlay_open(nodes: list[dict]) -> bool:
    """Is a sheet or modal covering the stream?

    Seen live: Whatnot's Options sheet (Report / Sound) puts a full-screen
    'Close sheet' scrim over everything, hiding the badge, chat and Leave. The
    bot read 'no giveaway running' behind it for twenty minutes. Either the
    explicit scrim, or any large clickable node with no stream marker on
    screen, counts.
    """
    if any(CLOSE_SHEET.match((n.get("content-desc") or "").strip()) for n in nodes):
        return True
    if find(nodes, IN_STREAM) is not None:
        return False
    for n in nodes:
        if (n.get("clickable") or "") != "true":
            continue
        b = bounds_of(n)
        if b and (b[2] - b[0]) * (b[3] - b[1]) >= 0.6 * 1080 * 2340:
            return True
    return False


def foreground_package(nodes: list[dict]) -> str:
    """The app that owns the screen, from the dump itself (every node carries
    its package). Empty string if the dump had no nodes."""
    counts: dict[str, int] = {}
    for n in nodes:
        p = n.get("package") or ""
        if p:
            counts[p] = counts.get(p, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def is_expanded(nodes: list[dict]) -> bool:
    """Is the giveaway card open, as opposed to the collapsed corner badge?

    The two layouts are distinguishable and this matters: tapping an already-open
    card closes it again. Collapsed puts the count and the word 'Entries' in
    separate sibling nodes; open renders a single 'N Entries' and shows the
    button.
    """
    if any(ENTRIES_INLINE.match((n.get("text") or "").strip()) for n in nodes):
        return True
    return find(nodes, ENTER_BUTTON) is not None or is_entered(nodes)


def badge_tap_point(nodes: list[dict]) -> tuple[int, int] | None:
    """Where to tap to open a collapsed giveaway card: the centre of the
    'Giveaway' LABEL, not of the whole badge.

    Measured on the phone: when the badge is gone, the 'More' button takes the
    same corner and its clickable area is [912,353][1056,508]. The union centre
    (~932,401) fell inside that, so a tap fired from a read taken seconds before
    the giveaway ended opened the Options sheet and hid the stream. The label
    centre (~932,330) is above that area, and tapping it was verified to open
    the card.
    """
    for n in nodes:
        if (n.get("text") or "").strip().lower() == "giveaway":
            b = bounds_of(n)
            if b is None:
                return None
            # Upper quarter of the label, not its centre: the badge sits ~30px
            # higher or lower depending on the stream's layout, and in the lower
            # layout the label centre is 6px inside the More button's area.
            return ((b[0] + b[2]) // 2, b[1] + (b[3] - b[1]) // 4)
    return None


def card_bounds(nodes: list[dict]) -> tuple[int, int, int, int] | None:
    """Union of the badge nodes and the count/label column: the tap target that
    expands the collapsed card. No badge node is itself clickable."""
    boxes = [b for b in (bounds_of(n) for n in _badge_nodes(nodes)) if b]
    for i, n in enumerate(nodes):
        if ENTRIES_LABEL.match((n.get("text") or "").strip()):
            for k in (i, i - 1):
                if 0 <= k < len(nodes):
                    b = bounds_of(nodes[k])
                    if b:
                        boxes.append(b)
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def prize_title(nodes: list[dict]) -> str:
    """The prize title from the EXPANDED card.

    Measured layout of the expanded panel, top of screen:
        '2  BOOSTER PACKS FOR FREE! #78'   <- the prize
        [Close]  [Giveaway icon]  '75 Entries'  'Terms & Conditions'
        'Follow Host & Enter Giveaway'     <- the button
    So the prize is the nearest real text node before the inline entries label.
    A collapsed badge has no prize text at all, and this returns "" for it rather
    than guessing: the longest text on screen would be a chat message.
    """
    skip = ("terms", "close", "giveaway", "entries", "entry")
    for i, n in enumerate(nodes):
        if not ENTRIES_INLINE.match((n.get("text") or "").strip()):
            continue
        for j in range(i - 1, max(-1, i - 6), -1):
            t = text_of(nodes[j])
            if len(t) < 4 or t.lower() in skip or ENTRIES_INLINE.match(t):
                continue
            if any(t.lower().startswith(k) for k in skip):
                continue
            return t
    return ""
