# SteamOS / Steam Deck

Use a Nintendo Switch 2 Pro Controller on a Steam Deck over Bluetooth, with
gyro, rumble, and player LEDs working natively in Steam Input.

This directory adds SteamOS support on top of
[trevlars/switch2-controllers-linux](https://github.com/trevlars/switch2-controllers-linux),
which does the Bluetooth LE work. Unofficial, not affiliated with Nintendo or Valve.

## What is different from upstream

| Area | Upstream | Here |
| --- | --- | --- |
| What Steam sees | generic uinput gamepad, gyro only through a DSU server | a Bluetooth Switch Pro Controller, gyro and rumble through Steam Input |
| Admin rights | expects passwordless `sudo` for `btmgmt` | none needed to run; one optional admin script |
| Install | Bazzite launchers, Decky plugin, edits Steam's Bluetooth setting | a venv and one user service |
| Rear grip buttons | not mapped | mapped (`BTN_GRIPL` / `BTN_GRIPR`) in uinput mode |

## How it works

```
Pro Controller 2 --BLE--> ngc bridge --/dev/uhid--> virtual "Pro Controller" (057e:2009, Bluetooth)
                                                       |-- kernel hid-nintendo: gamepad, IMU, battery, LEDs
                                                       `-- hidraw: Steam / SDL HIDAPI Switch driver
```

`ngc/procon_uhid.py` answers the original Pro Controller protocol (device info,
SPI flash calibration reads, report mode, IMU, rumble, LEDs) and streams
standard `0x30` input reports built from the Switch 2 reports. Rumble and
player LED commands from the host are translated back to the Switch 2
controller. Turning the controller off from the host puts the real one to sleep.

Motion axes and scale follow SDL's own Switch and Switch 2 drivers. The factory
gyro bias is read from the controller and removed.

## Install

```bash
git clone https://github.com/andyfreed/switch2-controllers-steamos.git ~/src/switch2-controllers-linux
cd ~/src/switch2-controllers-linux
bash steamos/install.sh
.venv/bin/python -m ngc pair        # hold Sync until the LEDs sweep
systemctl --user restart switch2-bridge.service
```

Press any button to wake a paired controller. It connects within a few seconds.

Do not run upstream's `scripts/install.sh` on SteamOS.

Pairing stores the Deck's address on the controller. It stops auto-connecting
to your Switch 2 until you pair it there again.

## Admin step (recommended)

```bash
sudo bash steamos/setup-admin.sh            # apply
sudo bash steamos/setup-admin.sh --uninstall   # revert
```

It makes two changes under `/etc`, which survives SteamOS updates:

1. **udev rule for `/dev/uhid`.** Lets the logged-in user create virtual HID
   devices. Without it the bridge falls back to a plain uinput gamepad and
   Steam gets no gyro. Any program running as that user can then create
   virtual input devices.
2. **Bluetooth LE connection interval** of 7.5 to 11.25 ms in
   `/etc/bluetooth/main.conf`, replacing the kernel default of 30 to 50 ms.
   The original file is backed up. Bluetooth restarts once. This is the
   adapter-wide default for LE devices that have no stored parameters.

Measured on a Steam Deck (SteamOS 3.8.28, kernel 6.18.50, BlueZ 5.83):

| | default | after admin step |
| --- | --- | --- |
| controller reports per second | 22 | 114 |
| typical gap between reports | 45 ms | 8 ms |

## Options

Set these as `Environment=` lines in
`~/.config/systemd/user/switch2-bridge.service`, then run
`systemctl --user daemon-reload` and restart the service.

| Variable | Default | Meaning |
| --- | --- | --- |
| `NGC_OUTPUT` | `auto` | `uhid`, `uinput`, or `auto` (uhid when `/dev/uhid` is accessible) |
| `NGC_EXTRA_KEYS` | `GL=KEY_PAGEUP,GR=KEY_PAGEDOWN,C=KEY_SCROLLLOCK` (set by the installer) | keyboard keys sent by the rear grip and C buttons |
| `NGC_EXTRA_BUTTONS` | empty | make extra buttons copy a controller button instead, e.g. `GL=L_STK,GR=R_STK,C=CAPTURE` |
| `NGC_NO_SUDO` | unset | `1` skips the `sudo btmgmt` calls without probing |
| `NGC_CONNECT_ATTEMPT_S` | `0.8` | seconds to wait per connection attempt |
| `NGC_CONNECT_ATTEMPTS` | `16` | attempts per wake |
| `NGC_IDLE_SLEEP_S` | `300` | sleep the controller after this long without button presses, `0` disables |

## Rear grip and C buttons

These three buttons cannot travel as gamepad buttons in virtual Pro Controller
mode. The bridge sends them as keyboard keys from a small virtual keyboard
named "Switch 2 controller extra buttons". Bind those keys in each game's own
keyboard settings.

Defaults are Page Up (left grip), Page Down (right grip) and Scroll Lock (C).
Change them with `NGC_EXTRA_KEYS`, using Linux key names such as `KEY_Q` or
`KEY_KP1`. Set it to an empty value to make the buttons do nothing.

Avoid `KEY_F13` to `KEY_F24`. Common keyboard layouts turn them into launcher
keys, which Windows games running under Proton do not receive.

`NGC_OUTPUT=uinput` exposes them as real gamepad buttons instead, at the cost
of gyro in Steam Input.

## Rumble

Rumble commands in the original Pro Controller format are decoded per grip
motor and per frequency band, then re-encoded the way SDL's Switch 2 driver
does it. Amplitude is capped at the limit SDL uses for this controller.

## Status and limits

Verified on one Steam Deck with one Pro Controller 2, in Desktop Mode:

- the kernel `hid-nintendo` driver binds and initialises the virtual device
- Steam lists it as "Nintendo Switch Pro Controller" on its HIDAPI driver
- buttons, sticks, d-pad, battery level, player LEDs
- accelerometer reads 1 g on the up axis at rest, gyro near zero
- rumble, sent both as kernel force feedback and as raw Pro Controller rumble
  reports (the form Steam uses), felt on the controller
- rear grip buttons arriving as keyboard keys

Not yet verified:

- Game Mode. Steam's background Bluetooth scanning may interfere with connecting.
- Rumble and gyro direction from inside a Steam game.
- Whether the first rumble block is the left grip motor, and how distinct the
  two frequency bands feel.
- The C button as a keyboard key.
- Joy-Con 2 and the NSO GameCube controller, which keep using the uinput path.

Known limits:

- The original Pro Controller protocol has no slots for the rear grip buttons
  or the C button, so Steam Input cannot see or rebind them. See
  [Rear grip and C buttons](#rear-grip-and-c-buttons).
- Reconnecting immediately after a dropped link can fail. Press a button.
- The protocol is reverse engineered. A controller firmware update can break it.
- ZL and ZR are digital on this controller.

## Uninstall

```bash
bash steamos/install.sh --uninstall
sudo bash steamos/setup-admin.sh --uninstall   # if you ran the admin step
rm -rf ~/.config/nso-gc
```
