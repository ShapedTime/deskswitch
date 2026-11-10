# deskswitch

Two Macs share one monitor and one Logitech keyboard and mouse. deskswitch makes the
monitor follow the keyboard, so the screen always shows the Mac you're actually using,
and you never land on a display that is connected but not visible.

It uses only what the hardware already exposes:

- **Monitor:** DDC/CI input select (VCP `0x60`), sent through
  [BetterDisplay](https://github.com/waydabber/BetterDisplay)'s CLI or
  [m1ddc](https://github.com/waydabber/m1ddc).
- **Where the keyboard is:** macOS drops a Bluetooth HID device from the IORegistry when
  it switches to another host. `ioreg` sees that without any extra permissions.
- **Keyboard and mouse switching (optional):** the Logi Options+ agent's local socket.
  The protocol was reverse engineered by
  [saimanish1/logitech-ipc-protocol](https://github.com/saimanish1/logitech-ipc-protocol).

Works with Logi Flow, Easy-Switch and Enhanced Easy-Switch. It's a single Python file
that needs only the standard library (3.9+), so the stock `/usr/bin/python3` is enough.
If the monitor accepts DDC on the input it isn't showing, only one of the two Macs needs
to run it.

## Quick start

```sh
brew install --cask betterdisplay && brew install waydabber/betterdisplay/betterdisplaycli
open -a BetterDisplay
python3 deskswitch.py calibrate        # guided: input codes, hidden-input test, presence test
python3 deskswitch.py follow --dry-run # Flow back and forth, watch the log
python3 deskswitch.py install          # LaunchAgent, log: ~/Library/Logs/deskswitch.log
```

The full setup guide, all commands, the one-button `switch`, pin/peek mode and
troubleshooting are in [docs/GUIDE.md](docs/GUIDE.md). `probe.sh` is a read-only
discovery script for new hardware.

Built and tested with a MacBook Pro (M1 Pro), a Huawei MateView GT 34 (`ZQE-CAA`), an
MX Keys S and an MX Master 4. Run the tests with `python3 test_deskswitch.py`.
