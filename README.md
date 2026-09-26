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
| **Emulator power** | *Start*, *Restart* and *Power off* for each emulator, usable whether or not the bot is running. Power off asks the emulator to shut itself down, waits until it is really gone, and unticks the device so the bot does not boot it straight back; killing the process is only a last resort after 60s. Restart is a cold boot, because a quick boot just restores the saved snapshot, glitch included. Both also end a hung emulator that adb does not list (9/24: two such leftovers held both emulators for 1.5 hours and neither button could end them) |
| **Rename** | Every device row has one. Two names: the device, and its *account label* (the Whatnot login; it tags log and Discord lines and owns the entry history, which moves with it). Applied live, no restart. The emulator's files on disk keep their original name |
| **Favorites** | Star the selected stream, or add a streamer by name. Favorites are looked up every minute even when they are too small for the category feeds, show as LIVE/offline, get +60 in the scanner (`favorite_bonus`) and are exempt from the viewer floor. Double-click a live one to sit in it. A blacklisted favorite still stays out of the scanner |
| **Start / Pause** | Pause leaves the devices alone without losing your place. The button reads Resume only when every account is paused by you: pressed during a safety or sign-in pause it pauses, and Resume then ends that pause at once too |
| **Alerts** | A red line under the top bar when something needs you: an emulator that will not start (the third failure in a row), a safety pause, a sign-in, or an account out of a stream for 15 minutes. Each alert is also a Windows notification from a red tray icon (click it to bring the window back) and a line in `alerts.log`, whether or not Discord is set up, and a Discord post when it is. One per incident, no repeats; *Dismiss* hides the line until the next one. *Settings > Bot > Send a test alert* checks once that they reach you on this PC (Focus Assist can hide the notification) |

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
to `config.toml` and used immediately. *Send a test alert* shows whether alerts reach you on this PC: a Windows
notification, the red banner, a line in `alerts.log`, and Discord when it is set up. It clears itself after 15 s.
*Leave a crowded stream right after its draw* is the crowd hop's one switch (live, log only, off; see *Architecture*),
next to its limit of voluntary moves per account per hour.

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

**Safety pause.** When an account's stream links keep failing on several sellers (5 failures on 4 sellers within
10 minutes; jswike's 9/22 suspension made 26 on 9 sellers in 20 minutes and nothing noticed), that account pauses:
its row reads SAFETY PAUSE, its stream is released, and one alert goes out with a screenshot. Only failures that point
at the account count: the emulator still up, and Whatnot saying the stream is live. After 20 minutes it tries one
stream, then 40, 80 and every 120 minutes, and goes back to work by itself when one opens; Resume ends it at once. It
survives an app restart: the new run keeps the pause and its next try, sends no second alert (the first is back in the
banner) and does not rejoin the stream. It never switches account, device or connection. `[safety] breaker = false`
turns it off.

**Sign-in pause.** When Whatnot shows its sign-in screen, in a stream or where a stream link should have opened one,
that account pauses at once: its row reads NEEDS SIGN-IN, its stream is released, and one alert goes out. It looks
again every 20 seconds and goes back to work by itself once you have signed in. On 9/24, after jswike's suspension
ended, the app was signed out, and only the screen in a stream was checked: 15 stream links on 4 sellers failed in 11
minutes, about one every 45 seconds, and nothing was entered until you signed in. The stream link still counts as a
failed open in the trial report; the safety pause does not count it, and a safety pause whose one try meets the sign-in
screen becomes a sign-in pause. `[safety] check_sign_in_on_open = false` goes back to trying the next stream.

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

**Which stream, and what is entered** (one rule set, `givvy/chooser.py`, agreed with the owner on 2026-09-19 after a day of patches that fought each other; the goal is to win as many pack-or-better giveaways as possible). Every stream is classified from the giveaways it lists, title *and* description: **pack_only** (every open giveaway is a pack or better: pack, sealed product, slab), **mixed**, **other** (no pack, not junk), and the never-joined kinds: junk only (cards, stickers, keychains, a stick of butter), buyers only, nothing listed, not read yet. A buyers giveaway beside open ones is just skipped. Order: not just left for being quiet > kind > **expected wins per hour** > fewer viewers, where expected wins = giveaways per hour *as observed* / expected entrants (2 + 0.18 x viewers, from measured medians). Measured on 550 visits: under 5 viewers only 23% of visits ever saw a giveaway, so "fewest viewers" alone parks the bot in dead streams; a 20-viewer stream with a pack every 5 minutes (2.1) beats a 10-viewer one with a pack every 11 (1.4). The 40 background listeners sit on the streams the scanner chooses among (they used to sit on the 40 biggest), and what each emulator sees goes into the same record. In a pack_only stream a card that is not a pack or better means: do not enter, leave, do not come back this broadcast. Mixed and other streams are a fallback, used only while no pack_only stream is free (checked every 30 s); there, anything that is not junk is entered. Patience is 90 s for a stream never seen to run anything, and one and a half of its own intervals (at most 15 min) for a stream with a known rhythm. Favorites are a watchlist and do not move the scanner. The word lists are `scoring.wanted_words` / `unwanted_words`.

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

## Architecture (2026-09-23)

**Screen reader helper** (`givvy/device/reader.py`). Every look at the screen used to start a fresh `uiautomator dump`
inside Android: 3.2 s a read on a live stream. The bot now keeps uiautomator2's helper (openatx, MIT; its `u2.jar` is
pushed to `/data/local/tmp` and run with `app_process`, no app installed) running in each emulator and asks it:
1.3-1.6 s on a busy stream, 0.2-0.4 s on a quiet one. The rest is Android walking the screen while video and chat
keep it busy. It does not save CPU worth mentioning: reads were ~2% of an emulator's CPU, the video is the rest.
Android allows one screen-reading session per device, so a failing helper is stopped and the old way used for a
minute; every helper call has a 12 s limit, so no account can freeze on a stuck read. `device.fast_reader = false`
turns it off; `--check-reader` times it on every connected device. The latest screen is kept in
`logs/screen-<device>.xml`.

**Stages** (`givvy/camper.py`). Each account is always in one stage: off, choosing, joining, watching, entering,
confirming, holding, leaving. Every change is logged with its reason (`[jgoblin22] watching -> entering: FREE PACK #3`)
and the window shows it. Everything about one giveaway lives in a `Turn` and everything about one stay in a stream in a
`Visit`, each replaced whole: the week's bugs were loose flags that one path reset and another did not.

**Restarts** (`givvy/camper.py`). On 9/23-24 every app start left both accounts 'choosing' for 79-89 s while the
stream list was built; 11 of 16 account-restarts were holding an entry, and 10 of 16 landed on another seller. Each
account's stream is now saved in `state.db`, and the first step after a start goes straight back to it, firing its
stream link again: the entry survives a rejoin (tradersshoprips #7 was won after one on 9/23). A stream the list does
not have yet is only rejoined when Whatnot says it is live; never one last seen over 15 minutes ago, a blacklisted
seller, or one another account holds. The category feeds reach the accounts before the ~250 followed-seller lookups.
And a stream missing from one stream list is left only when Whatnot says it is not live: a single miss used to be
enough, and 24 of 128 such exits 9/16-9/24 were provably false (4 by a stricter count).

**Crowd hop** (`givvy/hop.py`, `givvy/camper.py`). 9/19-9/24, stays whose first entrant read was over 30 took 26-28%
of camped time at about 0.11 pack wins/h, against about 0.63/h for first reads of 10 or fewer, and the bot never
re-checked a stream that kept running giveaways. Now the socket's 'ended' for the giveaway an account is in (the draw)
is judged: leave when a free stream of the same kind or better (pack-only only moves to pack-only), with a proven
rhythm and a giveaway the socket saw start at most 2 minutes before the move, promises 1.5x the expected pack wins per
hour, both streams on the one crowd model. Never out of a stream expecting fewer than 30 entrants at the draw, never
with an entry still open, never into a stream another account holds, and at most 3 voluntary moves (crowd hops and
pack-only upgrades) per account-hour, counted from the database so a restart does not reset it. Every judged draw is
one row in `hop_decisions`: stayed, would leave (log), left, or cancelled and why, with both streams' viewers, reads,
expected crowd, giveaways per hour, pack share and expected pack wins per hour. It raises joins from about 2 to at most
about 3 per account-hour: more automated movement, more Terms-of-Service exposure. Hence the cap and one switch,
`[hop] mode` (also in *Settings > Bot*): `live` (the default), `log` (write the decisions, never move) or `off` (as
1.1.x); `off` reverts the crowd hop alone, not the rest of 1.2.0.

Live, the next giveaway often opens within 14 s of a draw, so the decision is made early: while an entry is open the
account asks every 30 s whether it would leave if the draw came now, and if so enters nothing new in that stream until
its draw is judged (a card back after a flicker is told apart by the badge's tick; on a device that cannot read it,
nothing is held back). After the draw it stays 25 s (from when it decided, if the draw's news came late), entering
nothing, then leaves only when the screen agrees the draw happened: no badge in two reads, or a badge showing a giveaway
it is not in. "You're in" or the tick on any read after the draw, a win, or the switch turned off calls the move off,
and the card held back is entered at once. The rule is judged again at the moment of leaving, and the stream is claimed
then. In live mode a pack-only upgrade waits at the hourly cap too.

The pack-only upgrade (a mixed or no-pack stream left for a better pack-only one) never leaves with an entry open
either (`[pacing] upgrade_after_draw`): it used to go at the first read with no card, and the badge leaves the screen
for a few reads mid-giveaway, so 14 of 27 upgrades 9/20-9/24 came within 240 s of an entry. It waits for the draw (the
socket's, or a new giveaway on screen, at most `hold_timeout_s`); with the crowd hop live it is decided at that draw and
carried out like a hop, with the linger, the screen's proof and an `upgrade` row.

**What the bot learns, and results** (`givvy/chooser.py`, `givvy/metrics.py`). Stream rhythms are saved as they are
learned and read back on start. Entries record the card title, so gaps in a seller's numbering (#11, then #13) count
as misses; wins record the account. Each giveaway gets one row, with the highest entrant count seen on it and how the
entry was proven (direct, tick, late or redo). *Settings > Results* and `--report` show, per account: entered,
unconfirmed, skipped, missed, the share caught, wins, median entrants and rows per entry. They count giveaways, not
rows (1.1.x recorded some twice), and wins from the win history. For the 1.2.0 trial, every stay in a stream is one
row in `visits`: viewers at join, the first entrant count read there, when it began and ended, and why it ended (quiet,
ended, upgrade, pinned, paused...). A stream link that did not land is a zero-length row, so joins and failed opens
per hour can be counted. A stay cut off by a crash is closed at the last time the account was seen there (kept once a
minute).

**The trial report** (`givvy/trial.py`). 1.2.0 runs as a trial against the version before, on the same `state.db`.
The first Start on this version stores the trial's start with the version and a snapshot of the settings;
*Settings > Results > Start a new trial window now* moves it (after a change in the middle of the trial), and
`[trial] start` overrides it. *Settings > Results* and `--trial-report` (which writes `trial_report.txt`) show one
report that counts the baseline (the 7 days before this version first started, never before 9/20, when the pack-only
rules went live; moving the trial's start never moves it) and the trial with the same code, per account and in total:
active hours, entries per hour, pack wins per active hour and per account-day from the win history (only
tracker-confirmed wins when the Givvy Wins tracker is set, and then both windows stop an hour before its last read, or
at a win the bot saw that the tracker has not confirmed, with a warning to check that Givvy Wins is running), expected
wins from the one crowd model, the share of entries in 70+ viewer streams
and stream changes; from the stay log, joins, voluntary moves, failed opens and the crowd hop's decisions. It warns when
the accounts or the crowd hop's switch changed since the start, and says plainly what the counts can show: against
about 91 baseline pack wins even an endless trial only tells a change of about +40% / -29% from chance, so it is a
check for a big loss and that the mechanisms moved, never proof of the expected +6-9%.

**Record and replay** (`givvy/replay.py`). `device.record = true` (or `GIVVY_RECORD=1`) records every screen read,
command and tick screenshot to `recordings/` (about 12 MB an hour per emulator; not published: it holds other people's
chat). `tools/cut_recording.py` cuts a stretch into `tests/fixtures/replay/`, and the tests play it back through the
real device and decision code on a virtual clock. Open-loop: a replay checks what the bot concludes and does given
what really happened, not what the app would have done had it tapped elsewhere.

## Win Charts (2026-09-24)

**Total Wins Today** sits in big print in the middle of the top bar and goes up (green for a few seconds) as soon as
a win is seen. **Win Charts** opens a panel beside the activity log with four charts: givvies won by account, count
per date, givvies per day, and givvies per day by account. They come from the bot's own win history: every win it
sees, plus the wins in `givvy.log` from before this existed (read once on the first start). The bot only counts a
win it saw on screen, so it can be lower than your Whatnot order history. If you run a tracker of your own that
keeps Whatnot's order history in a SQLite `wins` table, `paths.givvy_wins_db` points the charts at it (checked every
5 minutes, read-only).

**Screenshots** in `screens/` are deleted after `paths.screens_keep_days` (7), and the oldest go first when the folder
is over `paths.screens_max_mb` (300). Only files the bot named are touched.

## Themes (2026-09-24)

The **Theme** box in the bottom bar switches the whole app at once and is remembered: **Light** (the Windows look),
**Dark**, and **Time Bomb TCG** (timebombtcg.com's navy, cream and gold, its logo beside Total Wins Today, a navy
title bar with a gold border). In Time Bomb TCG every win drops a bomb from the top of the window: it lands in the
middle, burns a 3-second fuse, and explodes while the window shakes, with '<account> Won <prize>' underneath.
*Test the blast* plays it on demand. It is skipped when Windows' Animation effects are off, or the window is
minimised.

**Nunu & Willump** (Freljord ice and Nunu Bot orange): on a win, Nunu & Willump roll across the window pushing a
snowball that grows as it goes, '<account> Won <prize>' pops up, and the ball bursts at the right edge. Its art is League
of Legends' Nunu Bot, rendered from the game's own files; it comes with the installer (in `themes/nunu/` beside the
program) and is not part of this source code, so a copy built from source offers the theme only when that folder exists.

*Nunu & Willump art © Riot Games. Whatnot Givvy isn't endorsed by Riot Games and doesn't reflect the views or
opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and
all associated properties are trademarks or registered trademarks of Riot Games, Inc.*

## Discord (optional)

Put a bot token and channel id in `config.toml` under `[discord]` and it reports
entries, follows, skips and wins there, mentioning you only for things that need
you. Without a token it just logs to `givvy.log`. Alerts (see *The window*) reach you either way; with
`alerts_channel_id` set they go to a channel of their own, with the mention, because the routine posts (about 700 a
day) would bury them.

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
`logs\emulator-<name>.log` (the launch before it in `emulator-<name>.prev.log`), a failed start is reported in about
two seconds in plain words on the device row ("CANNOT START: ..."), it is retried 1, 2, 4, 8 and 16 minutes after it
failed and then every 30 minutes, the third failure in a row raises one alert, and an emulator that says "already
running" while adb does not list it has the leftover ended and is cold-booted, once per try. **Settings > Emulator
check** lists, in the order they must be fixed: virtualisation in the BIOS, Windows Hypervisor Platform (off by
default on most PCs, and the usual cause), Microsoft's Visual C++ runtime, the Android download, each emulator, disk
and memory, with a one-click repair for the first failing one and a *Test: start the emulator* button. The page opens
by itself when a check fails, which is also how a fresh install gets set up.

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
| `pack_upgrade_ratio` | leave a mixed or no-pack stream for a pack-only one only when the pack-only one promises at least this many times the pack wins per hour (same crowd model on both sides, current viewers). 1.5 by default; `0` moves to any free pack-only stream, as 1.1.x did |
| `upgrade_after_draw` | that upgrade never leaves while our entry there may be open: only after its draw (the socket's, or a new giveaway on screen; at most `hold_timeout_s`), and with `[hop] mode = "live"` it is decided at the draw and goes after the linger, like a hop. `true` by default; `false` leaves at the first read with no card, as 1.1.x did |
| `quiet_max_per_h` | the most giveaways per hour a stream can score after 10+ minutes of listened silence, so a stream just proven quiet never ranks above one never watched (2.0/h). 0.5 by default; `3` gives back the 1.1.x formula |
| `quiet_memory_s` | how long "quiet while we listened" is kept after a stream leaves the 40 listener slots; time away does not count as quiet. 2700 (45 min) by default; `0` forgets at once, as 1.1.x did |
| `crowd_from_seen` | let the entrant counts our accounts read on cards refine a stream's expected crowd at the draw (2.0 + 0.90 x (median count + 1) + 0.54 x viewers). `true` by default; `false` uses viewers only (7 + 0.67 x viewers), the closest to 1.1.x, whose 2 + 0.18 x viewers ranks streams in about the same order, 3.5-3.7x lower |
| `one_row_per_giveaway` | when a card comes back in the stream within `same_giveaway_s` of our last row there and its entry count has not collapsed, it may be the giveaway already dealt with (the badge leaves the screen for a few reads mid-giveaway): read it before any tap, write no second row, and do not count it as a new giveaway for the stream's rhythm. `true` by default; `false` fast-taps and records it again, as 1.1.x did |
| `same_giveaway_s` | how recent that last row must be: 600 by default (repeats came 11-252 s after the first row). Keep it above `hold_timeout_s` |
| `skip_buyers_without_button` | an open buyers card (its words or the listing say so) with no Enter button is recorded 'skipped' once and held. `true` by default; `false` gives the 1.1.x four reads and a 'missed' row. Other cards with no button are always retried |
| `record_visits` | in `[trial]`: one `visits` row per stay in a stream and per failed stream open, for the trial report. Local bookkeeping only. `true` by default; `false` leaves those numbers blank |
| `auto_mark` | in `[trial]`: the first Start on this version, with no trial start stored, stores one (with the version and a snapshot of the settings). `true` by default |
| `start` / `end` | in `[trial]`: the trial window, as local `"YYYY-MM-DD HH:MM"` or `"YYYY-MM-DD"`. `start` overrides the stored start (to skip a warm-up hour, or restart the window after a change such as the crowd hop switched off; the automatic baseline stays before this version's first start); `end` closes the window (e.g. at a revert). Blank by default: the stored start, and now. An unreadable value is ignored and named in the report, never stopping the app |
| `baseline_start` / `baseline_days` | in `[trial]`: the baseline window. Blank / `7`: the 7 days before this version first started (stored once, never moved), never before 2026-09-20 00:00. `baseline_start` set: from then to the trial's start |
| `feeds_first` | in `[whatnot]`: on a start, hand the accounts the category feeds before the ~250 followed-seller lookups (one request each: they held the first pick 79-89 s on 9/23-24), then the full list. `true` by default; `false` publishes one list at the end, as 1.1.x did |
| `resume_after_restart` | save each account's stream in `state.db` (once a minute while it is there; cleared when it leaves on purpose) and go straight back to it after an app restart, firing its stream link again. A stream the list does not have yet is only rejoined when Whatnot says it is live. `true` by default; `false` goes back only after an emulator restart and only to a stream in the list, as 1.1.x did |
| `resume_within_s` | how recently the account must have been in that stream for a restart of the app or of its emulator to go back to it: 900 |
| `check_live_before_leaving` | a stream missing from one full stream list is left only when Whatnot says it is not live (asked once per list). `true` by default; `false` leaves on the first miss, as 1.1.x did |
| `mode` | in `[hop]`: the crowd hop's one switch (see *Architecture*), also in *Settings > Bot*, where a change applies at the next draw. `live` by default: judge every draw in the stream and leave a crowded one for a clearly better free one; `log` writes the decisions to `hop_decisions` and never moves; `off` (or anything else) is 1.1.x |
| `margin` | in `[hop]`: the free stream must promise this many times the expected pack wins per hour of the one we are in, same crowd model both sides: 1.5 (the proposal's range was 1.5-2) |
| `min_crowd` | in `[hop]`: never leave a stream expecting fewer entrants than this at the draw: 30, about a first read of 13-15 at 25-30 viewers (first reads of 10 or fewer won 0.63/h, 11-30 0.27/h) |
| `linger_s` / `fresh_s` | in `[hop]`: stay 25 s after the draw before leaving, entering nothing; the stream moved to must have a giveaway the socket saw start at most 120 s before the move (they run a median 204-251 s) |
| `max_per_hour` | in `[hop]`: voluntary moves per account in any hour, crowd hops and pack-only upgrades together: 3 (also in *Settings > Bot*; `0` = never hop). In live mode a pack-only upgrade waits at the cap as well; in log or off it is not capped |
| `breaker`, `breaker_failures`, `breaker_sellers`, `breaker_window_s` | in `[safety]`: the safety pause (see *Several accounts*): on by default, 5 failed stream opens on 4 sellers within 600 s. Replayed on the log from 9/16 to the morning of 9/24 it trips only on jswike's 9/22 suspension; a 3-in-2 rule also tripped on a healthy account |
| `breaker_recheck_s` / `breaker_recheck_max_s` | after a safety pause one stream is tried after 1200 s, twice as long after each failed try, at most 7200 s |
| `check_sign_in_on_open` | in `[safety]`: a stream link that lands on Whatnot's sign-in screen pauses the account for a sign-in (NEEDS SIGN-IN, one alert, back by itself once you have signed in), before the safety pause could count it. `true` by default; `false` tries the next stream, as before |
| `out_of_stream_min` | in `[alerts]`: one alert when an enabled account you did not pause has been out of a stream, or its loop has not ticked, this long (15 min; `0` = off). A boot, safety-pause or sign-in alert for the account comes instead |
| `windows_notify` | in `[alerts]`: a Windows notification for each alert and resolution (`true`). The banner and `alerts.log` work either way |
| `emulator_retry_s` / `emulator_retry_max_s` | in `[device]`: an emulator that failed to start is tried again 60 s after it failed, twice as long after each failure in a row, at most 1800 s; it never gives up |
| `emulator_alert_after` | failed starts in a row before one alert (3); the next good start resolves it |
| `emulator_kill_leftover` | when the emulator says "already running" and adb does not list it, or Power off / Restart meets one adb does not list, end the leftover emulator process for that AVD and cold-boot (`true`). It is never done when adb itself fails |
| `alerts_channel_id` | in `[discord]`: a channel for alerts only; blank = `channel_id` |
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
.venv\Scripts\python -m pytest -q             # 657 tests
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
