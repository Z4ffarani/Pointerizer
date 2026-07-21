# Pointerizer

Record mouse + keyboard actions on your PC, replay them on demand or on a schedule.

**Portable**: everything lives next to `Pointerizer.exe` — `recordings\` (your flows,
plain JSON) and `ui\` (generated UI assets). Copy the exe anywhere (USB stick included),
run it, done. The only things that are inherently system-side are the optional
sign-in launcher (Windows Startup folder) and schedules (Windows Task Scheduler) —
both reference the exe's absolute path, so if you move the folder, re-tick
"Run when I sign in" and re-create schedules from the new location.

## Use

Run `Pointerizer.exe` (or `python pointerizer.py`).

**Record**: hit **Record (F9)** — or just press **F9**. The cursor is centered, the window disappears, a red border frames every screen, and a floating pill appears at the bottom of the app's screen. You can drag it anywhere: before your first action it just restarts the clock silently; mid-flow it pauses at a checkpoint (border turns yellow) and recording resumes only when you hit **Continue (F8)** — either way the drag time never appears in the recording. Everything you do is captured — clicks, drags, typed text (accented characters included), special keys, hotkeys (Ctrl+C, and a lone **Windows key** to open Start), scrolling. Clicks on the pill itself are not recorded, and the app blocks interaction with its own window while recording. When you finish, a popup asks for a name — press **Enter** to accept the default (`Recording <date>`), or type your own.

While recording:
- **F8** (or the pill's **Checkpoint** button) — pauses (border turns yellow) and shows the actions since the last checkpoint. **F8** again continues from there; each **F7** press deletes the most recent action and stays paused; **F9** stops & saves. These keys work globally — the checkpoint window doesn't need to be focused.
- **F9** (or the pill's **Finish** button) — stop & save.

**Play**: click a recording to highlight it and hit **Play**, or just double-click it in the list. Playback also starts from a centered cursor, so a flow doesn't depend on where the pointer was.

Each recording row has a **checkbox** plus two icons: **pencil** (rename) and **clock** (schedule). Renaming migrates the sign-in launcher but drops any Task Scheduler schedule (it pointed at the old name) — re-create it after renaming.

**Selecting & deleting**: tick a flow's **checkbox** to select it for deletion (click it again to unselect); **Shift+click** a checkbox to select the whole range from the last one you clicked — no dragging. A red **trash icon** appears at the top-right of the list; click it and confirm (Enter confirms) to delete the selected flows. The **Del** key deletes them too. Deleting a flow also removes its schedule and sign-in launcher. During playback you get 2 seconds to bring the target window to front, then it replays with the original timing — the pointer glides smoothly to each target (no teleporting), and a gray border frames the screen while it runs.

**Abort a playback**: press **Esc** anytime (works for scheduled runs too), or slam the mouse into any screen corner.

## Scheduled runs

**Run at sign-in**: select a recording and tick **"Run when I sign in to Windows"** — it drops a launcher in your Startup folder, so the recording plays automatically shortly after you log in (plus a 3-second grace period). Untick to remove. Only one flow can run at sign-in — ticking a second one moves the sign-in slot to it. Likewise, creating a schedule that would fire at the same moment as another flow's schedule is blocked, since two replays can't share one mouse. (Windows blocks synthetic input on the lock screen, so "when the computer is on" in practice means "when you're signed in".)

**Run at specific times**: hit a recording's **clock icon** — pick hourly/daily/weekly (weekly shows day-of-week toggles), a start time, and how many times it should play per run. This creates a Windows Task Scheduler task (named `Pointerizer - <recording>`) that runs when you're logged in; reopen the dialog to replace or remove it.

Recordings are also plain JSON in the `recordings\` folder, so you can wire them to Task Scheduler manually for anything fancier:

```
"C:\path\to\Pointerizer.exe" --play "C:\path\to\recordings\myjob.json" --repeat 3
```

(You can also pass just the recording name: `--play myjob`.) It waits 3 seconds, then plays. The session must be unlocked — Windows blocks synthetic input on the lock screen.

## Limits (v1)

- No screen-content recognition: playback replays raw screen coordinates, so it needs the same resolution/layout as when recorded, with target windows in the same place.

## Run from source

```
pip install -r requirements.txt
python pointerizer.py
```

That's everything you need to record and play. `requirements.txt` is runtime-only.

## Dev & releasing

Building the exe/installer needs the build deps and Inno Setup on top of the runtime ones:

```
pip install -r requirements-dev.txt          # runtime deps + pillow + pyinstaller
winget install JRSoftware.InnoSetup          # once, if not already installed
python pointerizer.py --selfcheck
.\build.ps1
```

`build.ps1` runs the self-check, regenerates the icon (`pillow`), builds
`dist\Pointerizer.exe` (PyInstaller) and `dist\PointerizerSetup.exe` (Inno Setup).
The installer is per-user: no admin prompt, Start Menu entry, optional desktop
shortcut, proper uninstaller, and recordings stay in the app folder.

**Shipping an update**: bump `MyAppVersion` in [pointerizer.iss](pointerizer.iss),
run `.\build.ps1`, hand out the new `PointerizerSetup.exe`. Running it on a machine
with an older version upgrades in place (same `AppId`) — recordings and schedules
survive.
