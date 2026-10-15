<#
Open the installed Super Speech app, first rebuilding and reinstalling it from
this checkout when its source changed since the last install.

The desktop and Start menu shortcuts point here, so a click never opens a
stale build. Agents start the engine recorded by the installed app, so the
installed copy itself is what has to stay current, not a development run.

Run with -InstallShortcuts once to point both shortcuts at this script. The
installer recreates its own shortcuts on every install, so each install here
points them back.
#>
param(
    [switch]$Rebuild,
    [switch]$InstallShortcuts
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$appDir = Join-Path $repo "app"
$installDir = Join-Path $env:LOCALAPPDATA "Programs\super-speech-app"
$installedExe = Join-Path $installDir "Super Speech.exe"
$stampPath = Join-Path $appDir "release\installed-source.txt"

function Get-SourceFingerprint {
    # Every tracked or new file the build reads; ignored build output is excluded
    $files = git -C $repo ls-files -co --exclude-standard -- `
        app skills LICENSE THIRD_PARTY_NOTICES.md requirements-build.txt |
        Where-Object { Test-Path -LiteralPath (Join-Path $repo $_) }
    $hashes = $files | git -C $repo hash-object --stdin-paths
    $text = ($files + $hashes) -join "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    $digest = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    return -join ($digest | ForEach-Object { $_.ToString("x2") })
}

function Set-Shortcuts {
    $shell = New-Object -ComObject WScript.Shell
    $places = @(
        [Environment]::GetFolderPath("Desktop"),
        (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs")
    )
    foreach ($place in $places) {
        $link = $shell.CreateShortcut((Join-Path $place "Super Speech.lnk"))
        # conhost --headless keeps the quick path free of a console window
        $link.TargetPath = Join-Path $env:WINDIR "System32\conhost.exe"
        $link.Arguments = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        $link.WorkingDirectory = $appDir
        $link.IconLocation = "$installedExe,0"
        $link.Save()
    }
}

function Stop-ProcessesUnder([string]$folder) {
    # Files that a running app or engine holds open cannot be replaced
    Get-CimInstance Win32_Process |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($folder, "OrdinalIgnoreCase") } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}

function Install-FromSource([string]$fingerprint) {
    Write-Host "Super Speech's code changed since it was last installed."
    Write-Host "Rebuilding and reinstalling it. This takes a few minutes."
    Write-Host ""
    # A development run holds the engine the build replaces
    Stop-ProcessesUnder $appDir
    Push-Location $appDir
    try {
        npm run package:win
        if ($LASTEXITCODE -ne 0) { throw "the build failed (exit code $LASTEXITCODE)" }
        $version = (Get-Content package.json -Raw | ConvertFrom-Json).version
        $installer = Join-Path $appDir "release\$version\Super-Speech-Win-x64-$version-Setup.exe"
        Stop-ProcessesUnder $installDir
        $process = Start-Process $installer -ArgumentList "/S" -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "the installer failed (exit code $($process.ExitCode))" }
    } finally {
        Pop-Location
    }
    Set-Content -LiteralPath $stampPath -Value $fingerprint -NoNewline
    Set-Shortcuts
}

if ($InstallShortcuts) {
    Set-Shortcuts
    return
}

$fingerprint = Get-SourceFingerprint
$installed = if (Test-Path $stampPath) { (Get-Content $stampPath -Raw).Trim() } else { "" }
$stale = $fingerprint -ne $installed -or -not (Test-Path $installedExe)

if ($stale -and -not $Rebuild) {
    # Hand the slow path to a visible window so its progress and errors show
    Start-Process powershell.exe -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Rebuild")
    return
}

if ($stale) {
    try {
        Install-FromSource $fingerprint
    } catch {
        Write-Host ""
        Write-Host "Super Speech could not be updated: $_" -ForegroundColor Red
        Read-Host "Press Enter to open the older installed version, or close this window"
    }
}

Start-Process $installedExe
