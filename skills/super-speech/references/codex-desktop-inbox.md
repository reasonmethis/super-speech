# Codex desktop inbox adapter

Use this adapter only when the task runs in Codex desktop on Windows. The main
skill instructions define the portable inbox contract.

Start the background adapter before the first Speechicle that has an inbox:

```powershell
$thread = $env:CODEX_THREAD_ID
& "$skill\scripts\start-codex-inbox.ps1" -Inbox $inbox -ThreadId $thread
```

Require a JSON result whose `status` is `listening` or `already_listening`.
The adapter follows the inbox after the current turn ends. It uses the running
Codex desktop app to deliver each new message to the task identified by
`CODEX_THREAD_ID`.

Reuse the same inbox and task ID for the life of the task. Before offering
Reply, let the task become idle and verify that a test message wakes it.
