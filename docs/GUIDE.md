# deskswitch: setup guide

Makes the MateView GT 34 follow your MX Keys S and MX Master between the two MacBooks.
The ultrawide always shows the Mac that has the keyboard, and the joystick is no longer
needed to switch.

```
keyboard arrives on the USB-C Mac  ->  it asks the monitor for its own input   (pull)
keyboard leaves the USB-C Mac      ->  it asks the monitor for the HDMI input   (push)
hotkey / mouse button              ->  monitor + keyboard + mouse move together (switch)
```

**Status:** unit-tested offline, and calibrated on one setup (MacBook Pro M1 Pro +
MateView GT 34 + MX Keys S + MX Master 4). On other hardware, run `calibrate` first.

## Setup (on the Mac connected over USB-C)

```sh
# 1. DDC tool. Free for DDC use, and it can read which input is active.
brew install --cask betterdisplay
brew install waydabber/betterdisplay/betterdisplaycli
open -a BetterDisplay

# 2. Guided test. It writes ~/.config/deskswitch/config.ini for you.
python3 deskswitch.py calibrate
```

Before you run `calibrate`, put the Terminal on the **MacBook's built-in screen** and use
the **built-in keyboard**. During the test the ultrawide changes inputs and the MX keyboard
leaves the Mac. `calibrate` checks five things:

1. **This Mac's input code.** The monitor reports which input it is showing.
2. **The other Mac's input code.** This Mac switches the monitor to the HDMI Mac. If
   Huawei's code differs from the standard one, you can try other codes.
3. **The decisive test.** While the HDMI Mac is on screen, this Mac asks for its own
   input back. The question is whether the monitor obeys a source it isn't showing.
4. **Presence.** Whether the MX devices disappear from this Mac when they switch away.
5. **The Logi Options+ agent socket**, which `switch` uses.

Then try it out and install it:

```sh
python3 deskswitch.py follow --dry-run   # Flow back and forth, read the log
python3 deskswitch.py install            # LaunchAgent; log at ~/Library/Logs/deskswitch.log
```

### What the result of test 3 means

* **The monitor obeys the hidden input:** this Mac handles both directions on its own. It
  pulls the monitor when the keyboard arrives and pushes it to HDMI when you leave while
  active. The **HDMI Mac needs nothing installed**.
* **The monitor ignores it:** this Mac still pushes when you leave it. The HDMI Mac has to
  push back when you leave *it*. Install deskswitch there with `this_input`/`peer_input`
  swapped, `pull = no` and `push = yes`. DDC over the HDMI Mac's port then has to work too.
  BetterDisplay supports it on Apple Silicon built-in HDMI; through adapters it varies.

Push only fires if you used an input device within `active_window` seconds (10 s by
default) before the devices left. If the keyboard and mouse simply fall asleep, the
screen stays put.

## Commands

| Command | Does |
|---|---|
| `calibrate` | Guided test, writes the config |
| `status` | Presence, current input, Options+ socket, HID idle time |
| `ddc get` / `ddc set N` | Read or write VCP 0x60 directly |
| `follow [--dry-run]` | The daemon |
| `install` / `uninstall` | LaunchAgent for `follow` |
| `switch [--no-devices]` | Moves the monitor to the peer input, then the keyboard and mouse via the Options+ agent |
| `pause` / `resume` / `toggle [--notify]` | Pins the monitor: the daemon stops acting |
| `agent list` | Devices the Options+ agent knows about |

## Optional: one-button switch

1. Set `[logi] peer_host` to the HDMI Mac's Easy-Switch channel, counted from 0
   (channel 1 = 0).
2. In Shortcuts, create a shortcut with a **Run Shell Script** action:
   `/usr/bin/python3 ~/.local/share/deskswitch/deskswitch.py switch` (the copy `install` makes). Then go to Shortcut Details →
   **Add Keyboard Shortcut**. Optionally, map an MX Master button to that key combo in
   Options+.
3. If `agent list` fails because Options+ refuses outside clients (Logitech did this on
   Windows in 2.7), use `switch --no-devices` and press the keyboard's Easy-Switch key.
   With **Enhanced Easy-Switch** set up (Options+ → MX Keys S → Easy-Switch → Configure →
   link the mouse), the mouse follows.

## Pin / peek

* With `arrive_on = keyboard`, moving **only the mouse** (with the button under it) acts
  as a peek: the monitor stays where it is.
* `toggle --notify` pins the monitor until you toggle again. You can bind it the same way
  as `switch`.
* While pinned, the other Mac still has an invisible ultrawide. To hide it, set System
  Settings → Displays → ultrawide → *Use as: Mirror for Built-in Display* on that Mac.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ddc get` fails while this Mac is on screen | Make sure BetterDisplay is running. Check that `[monitor] name` matches the display name in `status` (macOS calls it `ZQE-CAA`). |
| Devices still listed in `ioreg` after switching away | `calibrate` falls back to `presence = optionsplus` when the agent answers. |
| Log says `Operation not permitted` on `deskswitch.py` | macOS stops background jobs from reading ~/Downloads, ~/Desktop and ~/Documents. Re-run `install`: it copies the script to `~/.local/share/deskswitch/` and runs it from there. |
| Monitor switches back and forth | Raise `debounce` or `cooldown`. |
| Push fires when you walk away | Lower `active_window`. |

## Caveats

* The Options+ agent protocol is undocumented. The reverse engineering is from
  github.com/saimanish1/logitech-ipc-protocol, and any Options+ update can break it. Only
  `switch` and `presence = optionsplus` depend on it.
* If a Mac is managed by an employer, check that you're allowed to install BetterDisplay
  and a LaunchAgent there. If test 3 passes, the HDMI Mac needs nothing installed.
