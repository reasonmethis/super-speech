# Super Speech

Super Speech gives AI coding agents private voice replies. Kokoro speech
synthesis runs on the user's computer, without an API key or per-word bill.

One agent reply becomes one Speechicle. A Speechicle stays one row and one
replay target even though the engine prepares it in smaller pieces. Queue
contains one Current Speechicle plus any Waiting Speechicles. Current is the
reply being prepared, played, paused, or held for playback. It sits between
Waiting and History at the playback boundary.
Finished, skipped, cleared, or manually archived Speechicles appear in History.

## Choose an installation

### Desktop app

Choose the desktop app for a tray icon, pause and resume controls, a visible
timeline, replay, reordering, voice changes, typed or pasted speech, and Light
or Dark appearance. Agents can also opt into an inbox so the user can send a
reply from a Speechicle's menu. The installer contains the app, speech engine,
model, voices, and agent skill. It does not need a separate Python or Node.js
installation.

The Windows x64 installer is tested locally from this repository. A public
download has not been published yet. The Electron app is designed to support
macOS, but the current macOS engine requires Apple Silicon and macOS 14 or
newer, and that package has not been tested on Mac hardware.

See [app/README.md](app/README.md) to build and test the desktop app.

### Headless skill

Choose the headless skill when spoken replies are useful but the desktop UI is
not. It installs the same speech engine, model, and voices inside the skill
folder. Python 3.11, 3.12, or 3.13 is required for installation.

See [SETUP.md](SETUP.md) for the complete headless installation guide.

## Use Super Speech

Ask your agent:

> Use Super Speech for your replies until I tell you otherwise

The agent sends the complete spoken reply to the platform launcher once. The
launcher uses the desktop engine when the app is installed. Otherwise, it uses
the engine packaged inside the headless skill. The app and the skill share the
same Queue and playback implementation.

## Desktop controls

- Pause stops at the current audio sample; Resume continues from that sample
- Click a Speechicle to expand its text, or double-click it to play it
- Click the voice beneath any Speechicle to play the same text with another
  installed voice
- Drag Waiting and History rows to reorder them
- Use a row's three-dot menu for the actions that apply there. Current has
  Pause or Resume while active and Play when stopped; Waiting and History have
  Play. All rows have Copy text. Delete moves Waiting into History and
  permanently removes History. A row also has Reply when its agent is
  listening on an inbox
- Clear all stops Current and moves active speech into History
- When Super Speech is idle, type or paste text into the main card, choose a
  voice, and add it as a Speechicle
- Click the main text area to follow the current piece in a larger view
- Use Settings to switch appearance or reveal extra voices hidden by default

Agents can attach a short source label to spoken replies. The app shows that
label beside the voice so simultaneous agent tasks remain distinguishable. An
agent can also attach a private inbox file. The app then lets the user send a
durable reply back to that task without exposing the file path in the window.
Agents attach an inbox only after arranging a live path that wakes or resumes
their task when a reply arrives. The file alone is storage and cannot wake an
agent.

## How it works

The shared Python engine owns text splitting, synthesis, playback, the speech
timeline, and recovery after an interrupted file change. Electron displays that
state and sends commands to the same engine.

Desktop state lives in `~/.super-speech/`. Headless state lives in `runtime/`
inside the installed skill, so the headless installation is self-contained.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the data model, process boundary,
startup rules, and code map.

## Project guides

- [Architecture](ARCHITECTURE.md)
- [Headless setup](SETUP.md)
- [Desktop development and packaging](app/README.md)
- [Agent skill contract](skills/super-speech/SKILL.md)
- [Product backlog](BACKLOG.md)

## License and redistribution

The Electron app and original Super Speech source are MIT licensed. The
self-contained installer also contains a separate speech engine with
GPL-licensed phonemization and eSpeak NG components, Apache-2.0 Kokoro model
files, and other permissively licensed libraries. Read
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before redistributing the
installer or building a closed-source commercial edition.
