# How Pointerizer works, and how to use it safely

Pointerizer records your mouse and keyboard and plays them back — on demand or on a
schedule. This note explains, in plain terms, what that means for your machine so nothing
here is a surprise.

## What it actually does

- **Controls your mouse and keyboard.** During playback it moves the pointer and presses
  keys for you. That's the whole point — but it also means a running flow is really typing
  and clicking on your computer.
- **Can start on its own.** If you tick *Run when I sign in to Windows*, or schedule a
  flow, Pointerizer places a launcher (a Startup shortcut or a Windows Task Scheduler
  task) so it runs without you opening it.
- **Only runs when you're signed in.** Windows blocks simulated input at the lock screen,
  and a scheduled run can't fire while the PC is off, asleep, or hibernating.
- **Always shows itself.** Every playback — including scheduled and sign-in runs — puts a
  border around the screen and a small pill on screen, so a flow is never a silent,
  invisible takeover of your mouse. Press **F7** to pause or **Esc** to cancel.

## Why your antivirus might flag it

A program that simulates input *and* sets itself to start automatically is the same shape
as some unwanted software, so security tools are cautious about it. Pointerizer is not
that — but two honest facts are worth knowing:

- **It isn't code-signed yet.** On first run, Windows SmartScreen may warn "unknown
  publisher." That warning reflects the missing signature, not anything the app does.
  Choose **More info → Run anyway** if you trust the download.
- **It does no networking.** Pointerizer never connects to the internet — it doesn't phone
  home, upload your recordings, or download anything. It reads and writes only local files.

## Your recordings are plain, readable files

Recordings are ordinary JSON files in a `recordings\` folder next to the app. That's what
makes them easy to back up, edit, and share — but it also means:

- **Don't record passwords or secrets.** Anything you type while recording is saved as
  plain, readable text. Treat a recording like a document, not a vault.
- **Only import recordings you trust.** An imported recording can move your mouse and type
  on your behalf. Pointerizer checks that a file is well-formed before playing it, but it
  can't know whether the *actions* inside are ones you want — so import from sources you
  trust, the same way you'd treat any downloaded script.
- **Where it's installed matters.** A per-user install lives in a folder your account can
  write to, which means other software running as you could in principle alter a recording
  that later runs on a schedule. This is inherent to any per-user tool; installing to a
  location only an administrator can modify reduces it.

## Stopping a run

- **Esc** cancels playback immediately.
- **Shove the mouse hard into any screen corner** — this is a built-in emergency stop.
- **F7** pauses; press it again to resume.

## Reporting a problem

Found a security issue? Please open an issue at
<https://github.com/Z4ffarani/Pointerizer/issues>. There's no formal disclosure process —
it's a small project — but genuine reports are welcome.
