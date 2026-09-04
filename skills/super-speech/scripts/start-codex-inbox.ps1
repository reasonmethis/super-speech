[CmdletBinding()]
param(
    [string]$Inbox,
    [string]$ThreadId,
    [switch]$Worker
)

$ErrorActionPreference = "Stop"
$pipePattern = '\\\\\\\\.\\\\pipe\\\\codex-browser-use-[0-9a-f-]+'
$maxFrameBytes = 8 * 1024 * 1024

function Get-RequiredValue {
    param(
        [string]$Value,
        [string]$EnvironmentName
    )

    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value
    }
    $environmentValue = [Environment]::GetEnvironmentVariable($EnvironmentName)
    if ([string]::IsNullOrWhiteSpace($environmentValue)) {
        throw "$EnvironmentName is required"
    }
    return $environmentValue
}

function Resolve-InboxPath {
    param([string]$Path)

    if (
        [string]::IsNullOrWhiteSpace($Path) -or
        $Path.Trim() -ne $Path -or
        -not [IO.Path]::IsPathRooted($Path) -or
        $Path.IndexOfAny([char[]]@("`r", "`n", [char]0)) -ge 0
    ) {
        throw "Inbox must be a valid absolute path"
    }
    $resolved = [IO.Path]::GetFullPath($Path)
    return $resolved
}

function Get-CodexPipeName {
    $server = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "codex.exe" -and
        $_.CommandLine -match "CODEX_APP_TOOLS_PIPE_PATH"
    } | Select-Object -First 1
    if ($null -eq $server) {
        throw "The Codex desktop app server is not running"
    }

    $match = [regex]::Match($server.CommandLine, $pipePattern)
    if (-not $match.Success) {
        throw "The Codex desktop app control pipe was not found"
    }
    $pipePath = $match.Value -replace '\\\\', '\'
    return $pipePath -replace '^\\\\\.\\pipe\\', ''
}

function Read-ExactBytes {
    param(
        [IO.Stream]$Stream,
        [int]$Length
    )

    $buffer = [byte[]]::new($Length)
    $offset = 0
    while ($offset -lt $Length) {
        $count = $Stream.Read($buffer, $offset, $Length - $offset)
        if ($count -le 0) {
            throw "The Codex desktop app closed its control pipe"
        }
        $offset += $count
    }
    return $buffer
}

function Send-CodexMessage {
    param(
        [string]$TargetThreadId,
        [string]$MessageId,
        [string]$Text
    )

    $pipeName = Get-CodexPipeName
    $pipe = [IO.Pipes.NamedPipeClientStream]::new(
        ".",
        $pipeName,
        [IO.Pipes.PipeDirection]::InOut,
        [IO.Pipes.PipeOptions]::Asynchronous
    )
    try {
        $pipe.Connect(3000)
        $callId = "super-speech-inbox-$MessageId"
        $request = @{
            jsonrpc = "2.0"
            id = 1
            method = "tools/call"
            params = @{
                arguments = @{
                    threadId = $TargetThreadId
                    prompt = $Text
                }
                callId = $callId
                namespace = "codex_app"
                threadId = $TargetThreadId
                tool = "send_message_to_thread"
                turnId = $callId
            }
        } | ConvertTo-Json -Depth 8 -Compress
        $body = [Text.Encoding]::UTF8.GetBytes($request)
        if ($body.Length -gt $maxFrameBytes) {
            throw "The inbox message is too large"
        }
        $prefix = [BitConverter]::GetBytes([uint32]$body.Length)
        $pipe.Write($prefix, 0, $prefix.Length)
        $pipe.Write($body, 0, $body.Length)
        $pipe.Flush()

        $lengthBytes = Read-ExactBytes -Stream $pipe -Length 4
        $length = [BitConverter]::ToUInt32($lengthBytes, 0)
        if ($length -gt $maxFrameBytes) {
            throw "The Codex desktop app returned an oversized response"
        }
        $responseBytes = Read-ExactBytes -Stream $pipe -Length $length
        $response = [Text.Encoding]::UTF8.GetString($responseBytes) | ConvertFrom-Json
        if ($null -ne $response.error) {
            throw "Codex rejected the inbox message: $($response.error.message)"
        }
        if ($response.result.success -ne $true) {
            throw "Codex did not accept the inbox message"
        }
    } finally {
        $pipe.Dispose()
    }
}

function Get-WatcherPaths {
    param([string]$Path)

    return @{
        Pid = "$Path.codex-wake.pid"
        Ready = "$Path.codex-wake.ready"
        Seen = "$Path.codex-wake.seen"
        Output = "$Path.codex-wake.log"
        Error = "$Path.codex-wake.error.log"
    }
}

function Test-WatcherProcess {
    param(
        [string]$PidPath,
        [string]$ScriptPath
    )

    if (-not (Test-Path -LiteralPath $PidPath -PathType Leaf)) {
        return $false
    }
    $watcherPid = 0
    if (-not [int]::TryParse((Get-Content -Raw -LiteralPath $PidPath).Trim(), [ref]$watcherPid)) {
        return $false
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $watcherPid"
    return (
        $null -ne $process -and
        $process.CommandLine -like "*$ScriptPath*" -and
        $process.CommandLine -match "-Worker"
    )
}

function Start-InboxWorker {
    param(
        [string]$ResolvedInbox,
        [string]$TargetThreadId
    )

    $paths = Get-WatcherPaths -Path $ResolvedInbox
    if (Test-WatcherProcess -PidPath $paths.Pid -ScriptPath $PSCommandPath) {
        [pscustomobject]@{
            status = "already_listening"
            inbox = $ResolvedInbox
            thread_id = $TargetThreadId
            pid = [int](Get-Content -Raw -LiteralPath $paths.Pid).Trim()
        } | ConvertTo-Json -Compress
        return
    }

    $parent = Split-Path -Parent $ResolvedInbox
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    if (-not (Test-Path -LiteralPath $ResolvedInbox -PathType Leaf)) {
        [IO.File]::WriteAllText($ResolvedInbox, "", [Text.UTF8Encoding]::new($false))
    }
    Remove-Item -LiteralPath $paths.Ready -Force -ErrorAction SilentlyContinue
    $powershellPath = (Get-Process -Id $PID).Path
    $oldInbox = $env:SUPER_SPEECH_CODEX_INBOX
    $oldThread = $env:SUPER_SPEECH_CODEX_THREAD_ID
    try {
        $env:SUPER_SPEECH_CODEX_INBOX = $ResolvedInbox
        $env:SUPER_SPEECH_CODEX_THREAD_ID = $TargetThreadId
        $process = Start-Process -FilePath $powershellPath -ArgumentList @(
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "`"$PSCommandPath`"",
            "-Worker"
        ) -WindowStyle Hidden -RedirectStandardOutput $paths.Output -RedirectStandardError $paths.Error -PassThru
    } finally {
        $env:SUPER_SPEECH_CODEX_INBOX = $oldInbox
        $env:SUPER_SPEECH_CODEX_THREAD_ID = $oldThread
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $paths.Ready -PathType Leaf) {
            [pscustomobject]@{
                status = "listening"
                inbox = $ResolvedInbox
                thread_id = $TargetThreadId
                pid = $process.Id
            } | ConvertTo-Json -Compress
            return
        }
        if ($process.HasExited) {
            $details = if (Test-Path -LiteralPath $paths.Error) {
                (Get-Content -Raw -LiteralPath $paths.Error).Trim()
            } else {
                ""
            }
            throw "The Codex inbox worker stopped during startup. $details"
        }
        Start-Sleep -Milliseconds 100
        $process.Refresh()
    }
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
    throw "The Codex inbox worker did not become ready"
}

function Invoke-InboxWorker {
    param(
        [string]$ResolvedInbox,
        [string]$TargetThreadId
    )

    $paths = Get-WatcherPaths -Path $ResolvedInbox
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $hasher.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes("$ResolvedInbox`n$TargetThreadId")
        )
    } finally {
        $hasher.Dispose()
    }
    $hash = [BitConverter]::ToString($hashBytes).Replace("-", "")
    $mutexName = "Local\SuperSpeechCodexInbox-$hash"
    $mutex = [Threading.Mutex]::new($false, $mutexName)
    $ownsMutex = $false
    try {
        $ownsMutex = $mutex.WaitOne(0)
        if (-not $ownsMutex) {
            return
        }
        [IO.File]::WriteAllText($paths.Pid, [string]$PID, [Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText($paths.Ready, [DateTime]::UtcNow.ToString("O"), [Text.UTF8Encoding]::new($false))
        $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        if (Test-Path -LiteralPath $paths.Seen -PathType Leaf) {
            foreach ($id in Get-Content -LiteralPath $paths.Seen) {
                if (-not [string]::IsNullOrWhiteSpace($id)) {
                    [void]$seen.Add($id.Trim())
                }
            }
        }

        $launcher = Join-Path $PSScriptRoot "super-speech.ps1"
        $ErrorActionPreference = "Continue"
        & $launcher listen-inbox $ResolvedInbox | ForEach-Object {
            try {
                $message = $_ | ConvertFrom-Json -ErrorAction Stop
                $messageGuid = [guid]::Empty
                if (
                    $message.version -ne 1 -or
                    $message.kind -ne "user_message" -or
                    $message.id -isnot [string] -or
                    -not [guid]::TryParse($message.id, [ref]$messageGuid) -or
                    $message.text -isnot [string] -or
                    [string]::IsNullOrWhiteSpace($message.text) -or
                    $seen.Contains($message.id)
                ) {
                    return
                }
                while ($true) {
                    try {
                        Send-CodexMessage -TargetThreadId $TargetThreadId -MessageId $message.id -Text $message.text
                        [void]$seen.Add($message.id)
                        Add-Content -LiteralPath $paths.Seen -Value $message.id -Encoding utf8
                        break
                    } catch {
                        [Console]::Error.WriteLine(
                            "[$([DateTime]::UtcNow.ToString('O'))] $($_.Exception.Message)"
                        )
                        Start-Sleep -Seconds 1
                    }
                }
            } catch {
                [Console]::Error.WriteLine(
                    "[$([DateTime]::UtcNow.ToString('O'))] Ignored invalid inbox entry: $($_.Exception.Message)"
                )
            }
        }
    } finally {
        Remove-Item -LiteralPath $paths.Ready -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
        if ($ownsMutex) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

$resolvedInbox = Resolve-InboxPath (Get-RequiredValue -Value $Inbox -EnvironmentName "SUPER_SPEECH_CODEX_INBOX")
$targetThreadId = Get-RequiredValue -Value $ThreadId -EnvironmentName "SUPER_SPEECH_CODEX_THREAD_ID"
$threadGuid = [guid]::Empty
if (-not [guid]::TryParse($targetThreadId, [ref]$threadGuid)) {
    throw "ThreadId must be a Codex task ID"
}

if ($Worker) {
    Invoke-InboxWorker -ResolvedInbox $resolvedInbox -TargetThreadId $targetThreadId
} else {
    Start-InboxWorker -ResolvedInbox $resolvedInbox -TargetThreadId $targetThreadId
}
