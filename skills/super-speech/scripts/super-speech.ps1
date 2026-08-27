$ErrorActionPreference = "Stop"

$skillDirectory = Split-Path -Parent $PSScriptRoot
$desktopRuntime = if ([string]::IsNullOrWhiteSpace($env:SUPER_SPEECH_HOME)) {
    Join-Path $HOME ".super-speech"
} else {
    $env:SUPER_SPEECH_HOME
}
$manifestPath = Join-Path $desktopRuntime "install.json"
$enginePath = $null

if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    try {
        $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
        if (
            $manifest.engine_path -is [string] -and
            -not [string]::IsNullOrWhiteSpace($manifest.engine_path) -and
            (Test-Path -LiteralPath $manifest.engine_path -PathType Leaf)
        ) {
            $enginePath = $manifest.engine_path
        }
    } catch {
        $enginePath = $null
    }
}

if ($enginePath) {
    & $enginePath @args
    exit $LASTEXITCODE
}

$headlessRuntime = Join-Path $skillDirectory "runtime"
$headlessEngine = Join-Path $headlessRuntime "venv\Scripts\super-speech-engine.exe"
if (-not (Test-Path -LiteralPath $headlessEngine -PathType Leaf)) {
    [Console]::Error.WriteLine(
        "Super Speech is not installed. Run this skill's scripts/install.py."
    )
    exit 1
}

$homeWasSet = Test-Path Env:SUPER_SPEECH_HOME
$modelDirectoryWasSet = Test-Path Env:SUPER_SPEECH_MODEL_DIR
$previousHome = $env:SUPER_SPEECH_HOME
$previousModelDirectory = $env:SUPER_SPEECH_MODEL_DIR

try {
    $env:SUPER_SPEECH_HOME = $headlessRuntime
    $env:SUPER_SPEECH_MODEL_DIR = Join-Path $headlessRuntime "models\kokoro"
    & $headlessEngine @args
    $exitCode = $LASTEXITCODE
} finally {
    if ($homeWasSet) {
        $env:SUPER_SPEECH_HOME = $previousHome
    } else {
        Remove-Item Env:SUPER_SPEECH_HOME -ErrorAction SilentlyContinue
    }
    if ($modelDirectoryWasSet) {
        $env:SUPER_SPEECH_MODEL_DIR = $previousModelDirectory
    } else {
        Remove-Item Env:SUPER_SPEECH_MODEL_DIR -ErrorAction SilentlyContinue
    }
}

exit $exitCode
