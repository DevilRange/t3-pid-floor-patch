# Raising the PID floor on the DIYLESS OpenTherm Thermostat 3

The Thermostat 3's PID controller has a minimum value (a "floor") baked into the firmware at 10.
I wanted mine higher (35) so the boiler keeps a sensible minimum water temperature while it's
actually heating. You can't just set this over the API, the controller snaps it back every cycle,
so the firmware itself has to be changed.

This is a small Python script that makes that change, plus a write-up of how I worked it out and
how you can put it on your own unit.

Heads up: this is a community mod, not something official from DIYLESS. It's fully reversible, but
you're editing firmware that runs a gas boiler, so take it slow and keep the backup you make in
step 2. If you're not comfortable with a command line, it might be worth waiting for an official
setting instead.

## The catch that makes this interesting

If you only change the floor from 10 to 35, the boiler starts short-cycling: it fires constantly
even when the room is already warm. That took a while to understand.

It turns out the firmware uses that same number 10 in two roles. It's the floor the PID output
can't drop below, and it's also the point where the firmware decides to switch central heating
OFF (when the computed water setpoint falls to 10 or lower). Those two 10s were the same value by
coincidence of design. So once the floor is 35, the setpoint can never fall back down to 10, and
heating never switches off. Hence the constant cycling.

The fix is to change the off check too, so it compares against the floor instead of a fixed 10.
After that, heating still switches off the instant the controller winds down to its minimum (room
satisfied), it just does that at 35 now instead of 10. Minimum flow temperature goes up, warm-up
is a bit quicker because the integral stays primed, and on/off behaves exactly like stock.

## How I worked it out

The firmware is closed source and runs on an ESP32-S3. I grabbed a copy of the firmware image and
loaded it into Ghidra (a free disassembler), with a community plugin that teaches it the ESP32's
Xtensa instructions, and went hunting for where the PID output gets clamped.

The floor turned out to be a single byte in the controller's memory, written by a tiny helper
function. Two spots in the code call that helper passing a hard-coded 10, those are the floor. The
off check was a separate piece of code comparing the setpoint against the constant 10.0. So the
whole patch is just: change those two 10s, and make the off check read the floor byte instead of
its own 10.

A couple of things that make this safe to try: on a stock unit the ESP32's secure boot and flash
encryption are both off, so a normal re-flash works and can be undone. And your Wi-Fi and settings
live in a separate area of memory that none of this touches.

## Why not use the new Hybrid algorithm?

Mainly because I don't have an outside temperature sensor, but also because to me it looks a little
bit over-engineered. I appreciate the thought that was put into it, but the supervisory layer, the
interactions between the learners, they all add-up to a lot of possible edge-cases which need testing.
For now, I like the PI algorithm, it's transparent, predictable and easy to understand.

## Which firmware it works with

It's for the OpenTherm Thermostat 3 firmware (the images DIYLESS publishes). I tested it on the 12
most recent versions, from 2.0 up to 3.0.0, and it patched every one of them.

It should keep working on future versions too, because it doesn't rely on fixed memory addresses,
those move around every build. Instead it looks for the code by its shape (the helper that stores
the floor, the places that call it, and the off check) and finds them wherever they moved. The one
constant 10.0 is even built two different ways depending on the build (older versions load it from
a table, newer ones compute it on the fly), and the script handles both. If a future build ever
changes things enough that it can't find a spot with full confidence, it stops and tells you,
instead of writing a bad file. It also re-reads its own edits afterward to double-check them.

## What you'll need

- A computer with Python 3 installed.
- esptool. Open a terminal and run: `pip install esptool`
- A USB cable that can carry data (some cheap ones only charge), with the thermostat plugged into
  your computer.

## Putting it on your thermostat

Below, wherever I write `COMx`, put your thermostat's port. On Windows it looks like `COM5` (check
Device Manager under Ports). On macOS or Linux it looks like `/dev/ttyACM0` or `/dev/ttyUSB0`.

### Step 1. Back it up

So you can always get back to exactly how it was:

```
esptool --port COMx get-security-info
esptool --port COMx read-flash 0 ALL backup.bin
```

The first line should say Secure Boot: Disabled and Flash Encryption: Disabled. Keep `backup.bin`
somewhere safe.

### Step 2. Get the firmware you want to patch

You have two choices. Pick whichever suits you.

**Option A, patch the firmware already on your thermostat (your own dump).** This keeps the exact
build that's running on your unit. You read it off the device and patch that copy:

```
esptool --port COMx read-flash 0x10000 0x3d0000 firmware.bin
```

**Option B, download a fresh copy from DIYLESS.** Use this if you'd rather put on a specific
published version, or just prefer a clean image. There's a little picker that lists the available
versions newest-first and downloads the one you choose:

```
python download_fw.py
```

It shows the latest five, lets you page back to older ones, and saves the file as
`firmware-<version>.bin` (it also prints the exact next command). To grab one without the menu:
`python download_fw.py 3.0.0`.

**The difference, in plain terms:** the two files are nearly identical. Your dump is your unit's own
copy of whatever version it's on; the download is a clean copy of a version you pick. The thermostat
reads its real network ID from a hardware fuse either way, so for normal use it makes no difference.
(Small technical note for Option A: the thermostat keeps two firmware slots internally and this
reads the main one. If you've been updating over the air and want to be certain which you're
grabbing, Option B lets you choose a known version instead.)

Either way you now have a firmware file to patch.

### Step 3. Patch it and check it

```
python patch_fw_auto.py firmware.bin patched.bin --floor 35
python -m esptool --chip esp32s3 image-info patched.bin
```

Change `35` to whatever minimum you want. If you used Option B, use the `firmware-<version>.bin`
name the downloader saved instead of `firmware.bin`. In the second command's output you want to see
the word valid next to both Checksum and Validation hash.

### Step 4. Flash it on

Grab one small boot file (it just tells the thermostat which slot to start), then write both:

```
curl -s -o boot_app0.bin "https://diyless.com/firmware/opentherm-thermostat3/3.0.0/boot_app0.bin"
esptool --port COMx write-flash 0x10000 patched.bin 0xe000 boot_app0.bin
```

The thermostat reboots by itself. Your Wi-Fi and settings are still there. You should see the
integral settle at 35 instead of 10, and the boiler should still switch off cleanly once the room
is up to temperature.

## Going back to stock

Any time you want the original firmware, just re-flash it with DIYLESS's own web flasher at
diyless.com/flasher, or restore the `backup.bin` you made in step 1.

## A small ask to DIYLESS

It would be lovely to have this as a real setting (a minimum CH flow temperature that the off check
respects), instead of a hard-coded 10. The actual change is only a few bytes. Happy to help if it's
useful.
