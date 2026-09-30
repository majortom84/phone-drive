# PhoneDrive

Plug your Android phone into your Mac and it shows up in Finder as a drive, just like a USB stick. Drag and drop files both ways. No app to open, no window to click through.

- **Offline.** No account, no cloud, no network calls.
- **Two ways to connect:** **adb** (fast and reliable, needs USB debugging on) or **MTP** (the phone's normal "File transfer" mode, no developer settings needed).
- **Browsing is free.** Opening a folder of 5,000 photos downloads nothing. A file is only copied when you actually open or copy it.

> **Status:** works day to day over adb. MTP support is **experimental**: its code is unit-tested but has not yet had a full real-phone check.

## How it works

A small background agent starts at login and checks for a phone every 2 seconds. When one appears, it mounts a [macFUSE](https://macfuse.github.io/) filesystem at `/Volumes/<phone name>` and opens it in Finder. Unplug the phone and the drive goes away.

Files download the first time they are read and upload when Finder finishes writing them. Finder thumbnails and Spotlight are kept from reading every file on the phone.

## Requirements

- macOS with [macFUSE](https://macfuse.github.io/) installed (allow its system extension when macOS asks, then restart)
- Python 3.9 or newer (`python3`)
- [Homebrew](https://brew.sh/), then:
  ```bash
  brew install android-platform-tools   # adb
  brew install libmtp                   # only needed for MTP
  ```

Works on both Intel and Apple silicon Macs: adb and libmtp are found on your `PATH` or in either Homebrew location.

> **Apple silicon and macFUSE:** macFUSE may ask you to allow system extensions from Recovery mode first ("Reduced Security"). Follow the [macFUSE instructions](https://github.com/macfuse/macfuse/wiki/Getting-Started) when macOS prompts you.

## Install

```bash
git clone https://github.com/<you>/phone-drive.git
cd phone-drive
./install.sh
```

This creates a virtual environment, installs the Python dependency ([mfusepy](https://pypi.org/project/mfusepy/)) and starts the login agent. Leave the folder where it is: the agent runs from it. **If you move the folder, run `./install.sh` again.**

Check it is running:

```bash
launchctl list | grep phonedrive        # shows a line with com.phonedrive.watcher
tail ~/Library/Logs/PhoneDrive.log      # shows "PhoneDrive watcher started"
```

### macOS pop-ups you may see

- **"Background Items Added"**: this is the PhoneDrive agent. Leave it allowed (*System Settings → General → Login Items*).
- **Python wants to access files on a removable volume**, or similar: click *Allow*. This is the drive reading your phone.
- **Notifications from Python/Script Editor**: allow them if you use MTP; that is how the "unlock your phone" message reaches you.

If you use another phone tool (MacDroid, Android File Transfer, OpenMTP), quit it first; only one program can talk to the phone over MTP at a time.

## Use

**With adb (recommended):**

1. Turn on Developer options: open *Settings → About phone* (on Samsung: *About phone → Software information*) and tap **Build number** 7 times. Enter your PIN if asked.
2. Open *Settings → Developer options* and turn on **USB debugging**.
3. Plug the phone in and tap **Allow** on the phone when it asks to allow USB debugging (tick "Always allow from this computer").

**With MTP:** leave USB debugging off. Plug in, unlock the phone, and choose *File transfer* in the USB notification. If the phone is locked, you get a Mac notification asking you to unlock it and tap *Allow*.

Within a few seconds a Finder window opens with `Internal storage` (and `SD card`, if you have one).

To stop using the drive, just unplug. Ejecting in Finder also works; it stays ejected until you plug the phone in again.

### Unplugging safely

You can pull the cable any time nothing is copying — no need to eject first. There is no write cache: each file is sent to the phone as soon as the copy finishes, so once Finder's progress bar is gone, your files are on the phone. Plug back in and the drive mounts again.

Don't unplug while a copy is running:

- **Copying to the phone:** the file being sent ends up missing or cut short, and Finder shows an error.
- **Overwriting a file over MTP:** MTP can't replace a file in place, so the old copy is deleted before the new one is sent. Unplugging in between loses both.
- **Moving a folder over MTP:** it is done as copy-then-delete, file by file. Unplugging halfway leaves the folder part-copied.

## Uninstall

```bash
./uninstall.sh
```

## Troubleshooting

The log is at `~/Library/Logs/PhoneDrive.log`. Failed file operations are logged there with the file name and reason.

| Problem | Try |
|---|---|
| No drive appears | Check the log. With adb, run `adb devices`: the phone should say `device`, not `unauthorized`. |
| Drive appears but is empty (MTP) | Unlock the phone and pick *File transfer*. |
| A phone tool "holds" the phone | Quit MacDroid, Android File Transfer, OpenMTP, etc. |

## Known limits

- Previewing a video in Finder (space bar or the preview pane) downloads the whole file.
- Phones don't store Mac permissions, dates or Finder tags. These are ignored when copying.
- macOS junk files (`.DS_Store`, `._*`) are never written to the phone.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

Tests use a fake phone; no device is needed.

## License

MIT. See [LICENSE](LICENSE).
