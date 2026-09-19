# Whatnot Givvy

Enters Whatnot giveaways for you by driving your docked Galaxy S24 Ultra over
USB. Giveaways can only be entered in the phone app, so the PC does the thinking
and the phone does the tapping.

**Start it:** double-click `Whatnot Givvy` on your Desktop, then press **Start**.

## The window

| Control | What it does |
|---|---|
| **Devices** | One row per phone or emulator. Untick one to leave it alone (remembered between runs). Devices that share an *account* are alternatives: the first one ticked and connected runs, so the docked phone is used when it is there and the emulator when it is not |
| **Mode** | *Stay in one stream*, or *Scan streams* and move only when the current one goes quiet. Applies to every account |
| **Stream list** | Pick an account in the selector, then double-click a stream to sit in it with that account. Shows game, viewers, giveaways seen, score, and which account is in it. The **Giveaways listed** column shows what each seller has queued as giveaways and how many of each are left (`FREE BOOSTER BOX x98`), fetched a couple of streams at a time in the background and refreshed every few minutes; `[buyers only]` marks buyer-appreciation giveaways you cannot enter without a purchase, and `now: ...` is the real prize title one of your accounts read off the screen there. Select a stream to see its whole list under the table. The list is grouped into viewer bands (1000+, 500-999, ... under 10) that expand and collapse and remember how you left them. The list re-checks itself: live streams every 60 s (`discovery_interval_s`, also in Settings; it is how the bot notices a stream ended, so longer means slower to move on) and each giveaway list every 3 minutes for the 120 best-scoring streams and every 10 minutes for the rest. Four requests run at once, so all ~600 lists are known within about two minutes of Start at a steady 1-2 requests a second; the readout beside Refresh shows how many are in. **Refresh** does both now, works with the bot stopped, and the readout beside it says when the last check was and when the next is due |
| **Emulator power** | *Start*, *Restart* and *Power off* for each emulator, usable whether or not the bot is running. Power off asks the emulator to shut itself down, waits until it is really gone, and unticks the device so the bot does not boot it straight back; killing the process is only a last resort after 60s. Restart is a cold boot, because a quick boot just restores the saved snapshot, glitch included |
| **Rename** | Every device row has one. Two names: the device, and its *account label* (the Whatnot login; it tags log and Discord lines and owns the entry history, which moves with it). Applied live, no restart. The emulator's files on disk keep their original name |
| **Favorites** | Star the selected stream, or add a streamer by name. Favorites are looked up every minute even when they are too small for the category feeds, show as LIVE/offline, get +60 in the scanner (`favorite_bonus`) and are exempt from the viewer floor. Double-click a live one to sit in it. A blacklisted favorite still stays out of the scanner |
| **Start / Pause** | Pause leaves the devices alone without losing your place |

It is a normal window: maximised the first time, then it comes back the size and
place you left it, and it minimises to the taskbar. **Hide activity** collapses the
log so the window can sit narrow beside the emulators (remembered too). Only one
copy can run at a time, because two would fight over the devices. The icon is drawn
by `tools/make_icon.py`.

## Settings

The **Settings...** button keeps the rarely-touched things off the main screen.

**Emulators** tab, one row per emulator:

| Setting | What it is |
|---|---|
| CPU cores, RAM | Written to that emulator's own `config.ini`. They are the most it may use, not a reservation. Whatnot alone is fine on 2 cores and 3072 MB |
| Frame rate | 60 or 30 fps (`hw.lcd.vsync`). **2 cores + 30 fps is the efficient setting and the default for new emulators.** Measured with three emulators playing live streams and the bot running: 4 cores/60 fps used 8.0 logical CPUs and 137 W package power; 2 cores/30 fps used 4.4 and 116 W, and entries took the same time (19.3 s before, 19.4 s after). Nearly all of an emulator's cost is Android drawing the stream (the Whatnot app ~1.8 cores, the compositor ~0.75, the video decoder ~0.5); the bot's own screen reading is ~0.07. Measured and useless: a smaller screen size, Windows efficiency mode, and the graphics setting |
| Graphics | `default`, `host` or `software`. Leave it on `default`. Measured with guarded runs (cold boot, same stream, 150 s settle, run voided if anything changed underneath it): the emulator already draws on the graphics card by itself, and an emulator costs **about 2 CPU cores** while a live stream plays whatever this says. It is passed as the `-gpu` launch flag, because the emulator ignores the `hw.gpu.*` keys in `config.ini`: with three different values written there, Android reported the same renderer and the same CPU use each time |
| Pin to cores | e.g. `28-31`. This is what actually dedicates CPU: the emulator's process is confined to those logical processors. *Suggest separate cores* hands out ranges from the top of the CPU down, leaving the low cores for Windows and games |
| Priority | `below normal` makes emulators give way to whatever else you are doing |

Cores, RAM and graphics need that emulator restarted (clean power-off, cold boot,
about 20 s); Apply offers to do it. Pinning and priority apply at once, are saved
in `config.toml` (`cpu_affinity`, `priority` on the device's block) and are
re-applied after every boot, because Windows forgets them at each launch. *Add
emulator account...* lives here too.

**Bot** tab: entries per hour per account, the smallest stream the scanner will
sit in, the favorite bonus, and how long scan mode waits in a quiet stream. Saved
to `config.toml` and used immediately.

## Several accounts

Each extra account runs on its own emulator, in its **own stream**. Whatnot
allows one entry per person per giveaway, so two of your accounts are never put
in the same stream: a camper claims a stream before opening it, ranking skips
claimed streams, and a double-click onto a stream another account holds is
refused. The point of more accounts is covering more streams at once, not more
entries in one draw. Nothing here hides the accounts from Whatnot.

```
.venv\Scripts\python -m givvy.main add-emulator emu3   # creates the AVD (~4 GB) and the config block
.venv\Scripts\python -m givvy.main accounts             # what is configured
```

Restart the app and it boots the new emulator in a window. Install Whatnot from
the Play Store and sign in on it **yourself, once**; the bot never types a
password. Until then that row reads "install Whatnot and sign in" and does
nothing. The hourly cap, follows and Discord lines are per account
(`[emu2] ENTERED ...`); the tariff blacklist is shared. Each emulator takes about
6 GB of RAM.

## How it works

It **camps in one stream** rather than hopping between them. That is the whole
design, and it came from measurement: chasing giveaways across streams went
**0 for 24**. Every single attempt arrived after the draw, because a giveaway is
open for a median of 204 seconds while detecting one costs up to 30 seconds and
driving the phone there costs another 20, and the card still has to be opened
before a button exists. Parked in a stream, the phone sees the card the moment
it appears.

Streams are ranked by game (Riftbound, then Pokémon, then Cyberpunk), by how
often that stream has actually been seen running giveaways, by viewer count for
odds, and by whether you already follow the seller. Streams below 40 viewers are
ignored by default (`min_viewers_to_camp`); an early version parked in a 4-viewer stream that never ran anything.
Discovery walks every page of each category feed (`feed_max_pages`, 12 pages of 50): measured live, the Pokemon feed
had 600 streams and the old single page of 24 stopped at 145 viewers, hiding the 480 streams under 40 viewers where the
odds are best. The odds term's cap scales with `odds_w` (Settings: 'Preference for small streams'), so 2 or 3 makes a
tiny stream with giveaways listed outrank a big one.

**The order streams are chosen in** is strict, not a sum of points: (1) never a buyers-only stream, which is dropped from the scanner whatever else it has going for it (Whatnot's buyer-appreciation flag, or the seller's words: "BUYERS", "must purchase"; "no purchase necessary" is read as the opposite, a case met on the live feed); (2) "pack" in the description of a giveaway anyone may enter; (3) fewest viewers. A stream listing something beats one not looked up yet, which beats one listing nothing, and a stream left for being dead goes to the back for 30 minutes (`quiet_cooldown_s`), or the smallest stream would be chosen for ever. The point score below (game, activity seen, favorites) only breaks ties now; to sit in a favorite regardless, double-click it. A stream that mixes buyers and open giveaways stays eligible, and the card on screen is read before every tap: one that says buyers is skipped. The window lists buyers-only streams last, marked as such.

Two more things feed the ranking, both added after watching it go wrong. **What the seller has listed**: a stream with giveaways you can enter gets +50 (`listed_bonus`), one that lists none loses 60, and one not looked up yet is neutral. With Riftbound worth 100 and the viewer floor lowered to 1, a 16-viewer Riftbound stream listing nothing had outscored every Pokemon stream. **Memory of dead streams**: a stream the bot left because nothing at all happened is pushed to the back for 30 minutes (`quiet_cooldown_s`); without that it picked the same dead stream five times in a row. Favorites are exempt from both. Right after a start the scanner waits up to 30 s for the giveaway lists rather than choose blind.

When a card appears it opens it, reads the **real prize name and entry count**
off the screen, re-scores, and only then taps. Below the confirm threshold it
backs out without entering. Whatnot's web API exposes neither the prize nor the
entry count, so this is the first moment either number is real.

A seller who ships from another country shows **"ATTENTION - additional tariffs
will apply"** before the stream loads. The scanner backs out, blacklists that
seller for good (it survives restarts) and picks somewhere else. The same seller
is also recognised from inside the stream by its "Shipping + Taxes + Tariffs"
listing line. A stream you double-click yourself is still joined. It never taps
*Don't show this again*, because that popup is how these sellers are spotted.

If the button says *Follow Host & Enter Giveaway*, one tap does both and the
follow is recorded.

It will not touch the phone while you are using it, caps entries at 12 per hour,
and randomises its tap timing. Inside an open card it moves as fast as the phone
allows: a screen read costs about 2.6s on a live stream (the reader waits for a
UI idle that a scrolling chat never provides), so the path from seeing a card
to a confirmed entry is three reads, roughly ten seconds, down from five reads
and fixed sleeps. The ENTERED line in the log records the measured figure.
It enters each giveaway once; only the app's own "You're in the Giveaway"
counts as confirmation.

## Discord (optional)

Put a bot token and channel id in `config.toml` under `[discord]` and it reports
entries, follows, skips and wins there, mentioning you only for things that need
you. Without a token it just logs to `givvy.log`.

## Sharing it: installer, updates, uninstall

```
.venv\Scripts\python installer\build.py                       # -> dist\WhatnotGivvySetup.exe (~17 MB) + .sha256
.venv\Scripts\python installer\release.py 1.1.0 "what changed"  # bump, test, build; add --publish to put it on GitHub
```

**Installer.** A standard Inno Setup installer around the app built as a *folder*. The first version was a
PyInstaller one-file exe, a program that unpacks itself into TEMP and runs from there, and a friend's Windows
Defender called it a trojan: that shape is what the heuristic looks for. The build now scans its own output with
Defender and refuses to finish on a detection. It is still unsigned, so SmartScreen shows "Windows protected your
PC" (*More info* -> *Run anyway*) on a copy downloaded with a browser; only a paid code-signing certificate removes
that. Per-user install, no admin. It only copies the program: the Android emulator is downloaded **from Google** by
the app on first run, because those files may not be redistributed.

**First run / "the emulator never came up".** That happened to a friend, with no reason shown anywhere: the
emulator's output was thrown away and the app waited four minutes in silence. Now the emulator's output is kept in
`logs\emulator-<name>.log`, a failed start is reported in about two seconds in plain words on the device row
("CANNOT START: ..."), it is not retried every tick, and **Settings > Emulator check** lists, in the order they
must be fixed: virtualisation in the BIOS, Windows Hypervisor Platform (off by default on most PCs, and the usual
cause), Microsoft's Visual C++ runtime, the Android download, each emulator, disk and memory, with a one-click
repair for the first failing one and a *Test: start the emulator* button. The page opens by itself when a check
fails, which is also how a fresh install gets set up.

**Updates.** *Settings > Check for updates* reads the newest GitHub release of `UPDATE_REPO` (in
`givvy/version.py`). Nothing installs by itself. On request it downloads the installer over HTTPS from github.com,
refuses to run it unless its SHA-256 matches the published one, then the app closes, Setup upgrades in place and
the app reopens. Settings, history and emulators are not part of the program files and are kept.

**Uninstall.** Windows *Settings > Apps > Installed apps > Whatnot Givvy*. The program folder (settings, history,
logs), shortcuts and the registry entry go. Emulators and the Android SDK live outside that folder, so you are asked;
nothing is ticked by default, closing the window keeps everything, only emulators and an SDK folder *this program
created* (they carry a marker file) are ever offered, and an emulator that is still in use is left whole rather than
half-deleted. Those rules exist because a test uninstall once cost the author a sign-in.

The build refuses to finish if `config.toml`, `state.db` or any value from your real config (Discord token, channel,
your user id, phone serial) is found in the payload, and a test refuses the same values in any tracked file, since
the source is public. A new install gets a clean config: emulator only, no Discord.

## Risk

Whatnot's Community Guidelines prohibit automated giveaway entry. Entries can be
voided and account access limited. There are no public reports of bans for it,
but the rule exists. This is your account and your call.

## Tuning

Everything lives in `config.toml`:

| Setting | Meaning |
|---|---|
| `min_score_confirm` | how good a giveaway must look, once its real prize and entry count are known |
| `min_viewers_to_camp` | ignore streams smaller than this |
| `switch_after_quiet_s` | scan mode: quiet spell before looking elsewhere |
| `switch_if_never_active_s` | shorter patience for a stream that has run nothing since you arrived |
| `max_entries_per_hour` | hard cap; unverified taps count too |
| `poll_interval_s` / `idle_poll_interval_s` | screen poll while camped (1.5s), easing to 3s after `idle_after_s` quiet |
| `enter_tap_delay_min_s` / `_max_s` | pause before the Enter tap only (0.15-0.4s); `tap_delay_*` covers other taps |
| `expand_settle_s` / `verify_settle_s` | poll step after tapping the badge / after tapping Enter |
| `game_w`, `prize_keywords` | scoring weights |

## Commands

```
.venv\Scripts\python -m givvy.app         # the window (--start presses Start for you)
.venv\Scripts\python -m givvy.main discover   # list live streams
.venv\Scripts\python -m givvy.main watch      # stream giveaway start/end events
.venv\Scripts\python -m givvy.main blacklist  # sellers the scanner will never pick
.venv\Scripts\python -m givvy.main unblock <seller>
.venv\Scripts\python -m pytest -q             # 302 tests
```

## What was measured, not assumed

The spec and plan in `docs/superpowers/` record the findings that shaped this,
each of which contradicted the obvious guess:

- An anonymous websocket never receives giveaway events. The only signal is a
  field flipping inside routine stream updates.
- The prize name cannot be recovered from the web API at all: the giveaway id is
  a different namespace from shop listing ids, and the upcoming list does not
  change when a giveaway starts.
- The app has no resource ids. Selectors key off text and geometry. The entry
  count sits in its own node beside the "Entries" label when the card is closed,
  and in a single "N Entries" node when it is open.
- The button reads *Enter Giveaway* only if you already follow the seller.
- After entering, the app says **"You're in the Giveaway"**, not "Entered".
  Missing that made the bot re-enter its own successful entries and log them as
  failures.
- The giveaway badge and the **More** button share the same corner; the app
  swaps them. A badge tap fired from a read a few seconds old, after the
  giveaway ended, lands on More and opens the Options sheet, whose scrim hides
  the whole stream. The bot now taps the upper quarter of the badge label,
  which is outside More's area in every captured layout, and dismisses any
  sheet it finds covering the stream.

Nothing is installed on the phone; `adb` alone covers it.
