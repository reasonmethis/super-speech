# Super Speech desktop controller

This directory contains the Electron desktop controller. The renderer is plain
TypeScript and CSS. A narrow preload bridge exposes status, pause, setup, and
window controls without giving the renderer Node.js or filesystem access.

The Python engine remains authoritative for synthesis, queue order, playback,
and the current sample cursor. Electron owns the app window, tray, installer,
and the future setup and engine-supervision flow.

## Development

```powershell
npm install
npm run dev
```

Useful focused checks:

```powershell
npm run check
npm run build
```

The controller reads `~/.super-speech/status.json` and creates or removes
`~/.super-speech/PAUSE`. The Kokoro drainer owns both file formats.

## Packaging

```powershell
npm run package:win
```

Electron Builder produces a current-user NSIS installer on Windows and is
configured for separate x64 and arm64 DMG builds on macOS. The version-zero
installer controls an engine installed through the repository setup guide.

The public release will freeze the Python engine as a platform-specific folder,
place it outside `app.asar`, and download the model into the writable runtime
home on first launch. Review `BACKLOG.md` before bundling the speech engine
because its transitive license set is separate from the MIT controller.
