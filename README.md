![Pointerizer](assets/banner.png)

## Install

Download **PointerizerSetup.exe** from the [latest release](https://github.com/Z4ffarani/Pointerizer/releases) and run it. It's a per-user install — no admin prompt, adds a Start Menu entry and an uninstaller. (It's unsigned, so on first run Windows SmartScreen may warn: **More info → Run anyway**.)

Your recordings are plain JSON in a `recordings\` folder next to the app.

**To update** — download the newer `PointerizerSetup.exe` and run it from wherever it landed (your Downloads folder is fine). You don't need to put it in, or point it at, the folder Pointerizer is already installed in: the installer recognises the existing install and upgrades it in place, keeping your recordings and schedules. Close the app first if it's running.

## Use

**Record** — hit **Record (F9)** (or just press **F9**). The cursor centers, the screen gets a red border, and a draggable pill appears. Everything you do is captured: clicks (including **Ctrl/Shift/Alt+click** and the mouse **back/forward** buttons), drags, typed text (accents and **Alt+numpad** codes included), hotkeys (Ctrl+C…), a lone **Windows key**, and scrolling (vertical, horizontal, and **Ctrl+scroll** to zoom). When you finish, a popup asks for a name — **Enter** accepts the dated default, or type your own.

- **F8** — checkpoint: pause and review actions since the last one. F8 again continues; **F7** deletes the last action; **F9** stops & saves. (These work globally.)
- Drag the pill out of your way whenever you like — it doesn't interrupt anything. The click that moves it and the time you spend dragging are both discarded, and recording carries straight on from your last real action. Use **F8** when you actually want to stop and review.

**Timing** — the save popup asks how the flow should replay:

- **Keep my original timing** (default) — every pause you took is reproduced exactly.
- **Unticked** — every step replays a fixed **0.35s** apart. Faster, and it strips out the thinking time. The gap is deliberately not zero: actions fired back-to-back tend to outrun the app you're driving, so 0.35s gives each one time to land. Your original timings stay in the file, so nothing is destroyed by choosing this.

**Play** — click a recording and hit **Play**, double-click it, or select it and press **Enter**. You get 2 seconds to bring your target window to front, then it replays (the pointer glides to each target). Playback also starts from a centered cursor. A grey border frames the screen and a pill shows the controls:

- **F7** — pause. The border turns yellow and the pill says so; **F7** again resumes from exactly where it stopped. Time spent paused isn't charged against the next step's delay.
- **Esc** — cancel outright. Slamming the mouse into a screen corner also aborts.

**Reorder** — drag a flow up or down to arrange the list however you like; the order is remembered.

**Select & delete** — tick a flow's **checkbox** (click again to unselect); **Shift+click** a checkbox to select a range. Ticking a box also makes that flow the active one, so the sign-in toggle applies to it. A red **trash icon** appears top-right — click it (or press **Del**) to delete the selected flows. Each row also has **pencil** (edit) and **clock** (schedule) icons.

**Edit** — the **pencil** opens a recording to rename it, switch its timing, and **delete individual steps** (select a step and hit Delete). Changes apply only when you **Save**; **Esc** closes without touching the recording.

**Share** — **Export** saves the selected flow to a file; **Import** brings recordings in. They're just JSON, so you can email or version them like any document. Imported files are checked for validity, but only import ones you trust — a recording can move your mouse and type for you. See [SECURITY.md](SECURITY.md) for the full safety note.

## Scheduled runs

- **Run at sign-in** — tick **"Run when I sign in to Windows"** (drops a launcher in your Startup folder). Only one flow can hold the sign-in slot at a time. It waits **1 minute** after you sign in before replaying, so the desktop has time to finish loading — the pill counts that down (*Starting in 0:59*), and **Esc** cancels during the wait. The launcher asks the app for that delay rather than storing it, so if a future version changes it, your existing sign-in entry picks up the new value automatically.
- **Run at set times** — the **clock icon** creates a Windows Task Scheduler task (hourly/daily/weekly, a start time, and plays-per-run). Reopen the dialog to change or remove it.

Scheduled runs show the same grey border and control pill as a normal playback, so a flow firing on its own is never a silent takeover of your mouse — **F7** pauses it and **Esc** cancels it, exactly as when you start it by hand.

**The PC has to be on and signed in.** A scheduled run cannot fire while the machine is off, asleep, or hibernating — Task Scheduler has nothing to run it on. It also can't fire at the lock screen, because Windows blocks synthetic input there. A task whose time passed while the PC was off simply doesn't happen; it won't queue up and fire later.

Recordings are plain JSON, so you can also wire them to Task Scheduler yourself:

```
"C:\path\to\Pointerizer.exe" --play "C:\path\to\recordings\myjob.json" --repeat 3
```

## Languages & keyboard layouts

| Input | Record | Replay |
|---|:---:|:---:|
| Latin — English, Portuguese, French, German, Spanish, Nordic, Polish, Czech… | ✅ | ✅ |
| Accented letters typed with dead keys — `^`+`e` → `ê`, `~`+`a` → `ã`, `´`+`o` → `ó` | ✅ | ✅ |
| Direct non-Latin layouts — Cyrillic, Greek, Hebrew, Arabic | ✅ | ✅ |
| Emoji and other characters outside the basic plane | — | ✅ |
| Chinese, Japanese, Korean typed through an **IME** | ❌ | ✅ |

**Replay is layout-independent.** Text is typed as raw Unicode rather than as key presses, so a recording made on one machine reproduces the same characters on any other, whatever layouts are installed there.

**Recording follows your keyboard.** Dead keys are resolved against the layout of the window you're typing into, so accents are stored as the single character they produce (`ê`, not `^e`) — and switching layouts mid-recording is handled.

**IMEs are the exception.** Chinese, Japanese and Korean input works by typing phonetically and picking from a candidate list; only the phonetic keystrokes reach Pointerizer, never the characters you chose. Worse, candidate order changes as the IME learns, so the same recording would drift over time. Replaying such text is fine — it's the capture that can't work.

## Limits

Playback replays raw screen coordinates — it needs the same resolution/layout as when recorded, with target windows in the same place. No screen-content recognition.

## Develop

Run it from source — no build needed:

```
git clone https://github.com/Z4ffarani/Pointerizer
cd Pointerizer
pip install -r requirements.txt   # runtime deps
python pointerizer.py
```

`python pointerizer.py --selfcheck` runs the built-in sanity checks.

Build the installer yourself (needs `requirements-dev.txt` + [Inno Setup](https://jrsoftware.org/isinfo.php)):

```
pip install -r requirements-dev.txt
winget install JRSoftware.InnoSetup
.\packaging\build.ps1
```

`packaging\build.ps1` runs the self-check, regenerates the icon, and produces `dist\Pointerizer.exe` (PyInstaller) and `dist\PointerizerSetup.exe` (Inno Setup). For a new version, bump `MyAppVersion` in [packaging/pointerizer.iss](packaging/pointerizer.iss) before building — the `AppId` never changes, so the new installer upgrades any older one in place.

### Layout

```
pointerizer.py        the whole app (single file)
assets/               icon, banner, chevrons, bundled Ubuntu font
packaging/            build.ps1, make_icon.py, Inno Setup script
requirements*.txt     runtime / build deps
```
