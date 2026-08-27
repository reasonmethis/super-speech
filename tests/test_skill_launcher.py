from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
POWERSHELL_LAUNCHER = (
    ROOT / "skills" / "super-speech" / "scripts" / "super-speech.ps1"
)
SHELL_LAUNCHER = (
    ROOT / "skills" / "super-speech" / "scripts" / "super-speech.sh"
)
INSTALLATION_ERROR = (
    "Super Speech is not installed. Run this skill's scripts/install.py."
)


def powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("PowerShell is not available")
    return executable


def copy_powershell_launcher(skill: Path) -> Path:
    launcher = skill / "scripts" / POWERSHELL_LAUNCHER.name
    launcher.parent.mkdir(parents=True)
    shutil.copy2(POWERSHELL_LAUNCHER, launcher)
    return launcher


def run_powershell_launcher(
    launcher: Path,
    *arguments: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def write_python_probe(path: Path) -> None:
    path.write_text(
        """\
import json
import os
import sys
from pathlib import Path

capture = Path(sys.argv[1])
exit_code = int(sys.argv[2])
capture.write_text(json.dumps({
    "arguments": sys.argv[3:],
    "home": os.environ.get("SUPER_SPEECH_HOME"),
    "models": os.environ.get("SUPER_SPEECH_MODEL_DIR"),
}), encoding="utf-8")
raise SystemExit(exit_code)
""",
        encoding="utf-8",
    )


def test_powershell_launcher_prefers_valid_desktop_manifest_and_forwards_arguments(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skill"
    launcher = copy_powershell_launcher(skill)
    runtime = tmp_path / "desktop-runtime"
    runtime.mkdir()
    probe = tmp_path / "probe.py"
    capture = tmp_path / "capture.json"
    write_python_probe(probe)
    (runtime / "install.json").write_text(
        json.dumps({"engine_path": sys.executable}),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "SUPER_SPEECH_HOME": str(runtime),
        "SUPER_SPEECH_MODEL_DIR": "desktop-models",
    }

    result = run_powershell_launcher(
        launcher,
        str(probe),
        str(capture),
        "23",
        "two words",
        "--voice=af_heart",
        environment=environment,
    )

    assert result.returncode == 23
    assert json.loads(capture.read_text(encoding="utf-8")) == {
        "arguments": ["two words", "--voice=af_heart"],
        "home": str(runtime),
        "models": "desktop-models",
    }


@pytest.mark.parametrize("manifest_contents", [None, "not json", "{}"])
def test_powershell_launcher_falls_back_to_skill_runtime(
    tmp_path: Path,
    manifest_contents: str | None,
) -> None:
    skill = tmp_path / "skill"
    launcher = copy_powershell_launcher(skill)
    runtime = tmp_path / "desktop-runtime"
    runtime.mkdir()
    if manifest_contents is not None:
        (runtime / "install.json").write_text(manifest_contents, encoding="utf-8")
    headless_engine = (
        skill / "runtime" / "venv" / "Scripts" / "super-speech-engine.exe"
    )
    headless_engine.parent.mkdir(parents=True)
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", headless_engine)
    environment = {**os.environ, "SUPER_SPEECH_HOME": str(runtime)}

    result = run_powershell_launcher(
        launcher,
        "/d",
        "/c",
        "set SUPER_SPEECH_HOME & set SUPER_SPEECH_MODEL_DIR & exit /b 19",
        environment=environment,
    )

    assert result.returncode == 19
    assert f"SUPER_SPEECH_HOME={skill / 'runtime'}" in result.stdout
    assert (
        f"SUPER_SPEECH_MODEL_DIR={skill / 'runtime' / 'models' / 'kokoro'}"
        in result.stdout
    )


def test_powershell_launcher_reports_one_installation_error(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    launcher = copy_powershell_launcher(skill)
    environment = {**os.environ, "SUPER_SPEECH_HOME": str(tmp_path / "missing")}

    result = run_powershell_launcher(launcher, "status", environment=environment)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == INSTALLATION_ERROR


def bash() -> str:
    if os.name == "nt":
        executable = shutil.which("wsl")
        if not executable:
            pytest.skip("WSL is not available")
        return executable
    executable = shutil.which("bash")
    if not executable:
        pytest.skip("Bash is not available")
    return executable


def shell_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    relative = path.resolve().as_posix()[3:]
    return f"/mnt/{path.drive[0].lower()}/{relative}"


def make_shell_executable(path: Path) -> None:
    path.chmod(0o755)
    if os.name == "nt":
        subprocess.run(
            [bash(), "-e", "chmod", "+x", shell_path(path)],
            check=True,
            capture_output=True,
            text=True,
        )


def copy_shell_launcher(skill: Path) -> Path:
    launcher = skill / "scripts" / SHELL_LAUNCHER.name
    launcher.parent.mkdir(parents=True)
    shutil.copy2(SHELL_LAUNCHER, launcher)
    make_shell_executable(launcher)
    return launcher


def write_shell_probe(path: Path) -> None:
    path.write_text(
        """\
#!/bin/sh
{
    printf 'home:%s\\n' "${SUPER_SPEECH_HOME-unset}"
    printf 'models:%s\\n' "${SUPER_SPEECH_MODEL_DIR-unset}"
    for argument do
        printf 'arg:%s\\n' "$argument"
    done
} > "$LAUNCHER_CAPTURE"
exit "$LAUNCHER_EXIT"
""",
        encoding="utf-8",
        newline="\n",
    )
    make_shell_executable(path)


def run_shell_launcher(
    launcher: Path,
    *arguments: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    command = [bash(), shell_path(launcher), *arguments]
    process_environment = environment
    if os.name == "nt":
        forwarded_names = (
            "PATH",
            "HOME",
            "SUPER_SPEECH_HOME",
            "SUPER_SPEECH_MODEL_DIR",
            "FAKE_DESKTOP_ENGINE",
            "LAUNCHER_CAPTURE",
            "LAUNCHER_EXIT",
        )
        forwarded = [
            f"{name}={environment[name]}"
            for name in forwarded_names
            if name in environment
            and not (name == "PATH" and not environment[name].startswith("/"))
        ]
        command = [
            bash(),
            "-e",
            "/usr/bin/env",
            "-u",
            "SUPER_SPEECH_HOME",
            "-u",
            "SUPER_SPEECH_MODEL_DIR",
            *forwarded,
            "/bin/bash",
            shell_path(launcher),
            *arguments,
        ]
        process_environment = os.environ.copy()
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=process_environment,
    )


def test_shell_launcher_prefers_valid_desktop_manifest_and_forwards_arguments(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skill"
    launcher = copy_shell_launcher(skill)
    runtime = tmp_path / "desktop-runtime"
    runtime.mkdir()
    engine = tmp_path / "desktop-engine"
    capture = tmp_path / "capture.txt"
    write_shell_probe(engine)
    (runtime / "install.json").write_text(
        json.dumps({"engine_path": shell_path(engine)}),
        encoding="utf-8",
    )
    tools = tmp_path / "tools"
    plutil = tools / "plutil"
    tools.mkdir()
    plutil.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$FAKE_DESKTOP_ENGINE\"\n",
        encoding="utf-8",
        newline="\n",
    )
    make_shell_executable(plutil)
    environment = {
        **os.environ,
        "PATH": f"{shell_path(tools)}:/usr/bin:/bin",
        "SUPER_SPEECH_HOME": shell_path(runtime),
        "SUPER_SPEECH_MODEL_DIR": "desktop-models",
        "FAKE_DESKTOP_ENGINE": shell_path(engine),
        "LAUNCHER_CAPTURE": shell_path(capture),
        "LAUNCHER_EXIT": "29",
    }

    result = run_shell_launcher(
        launcher,
        "two words",
        "--voice=af_heart",
        environment=environment,
    )

    assert result.returncode == 29
    assert capture.read_text(encoding="utf-8").splitlines() == [
        f"home:{shell_path(runtime)}",
        "models:desktop-models",
        "arg:two words",
        "arg:--voice=af_heart",
    ]


@pytest.mark.parametrize("manifest_contents", [None, "not json"])
def test_shell_launcher_falls_back_to_skill_runtime(
    tmp_path: Path,
    manifest_contents: str | None,
) -> None:
    skill = tmp_path / "skill"
    launcher = copy_shell_launcher(skill)
    runtime = tmp_path / "desktop-runtime"
    runtime.mkdir()
    if manifest_contents is not None:
        (runtime / "install.json").write_text(manifest_contents, encoding="utf-8")
    headless_engine = skill / "runtime" / "venv" / "bin" / "super-speech-engine"
    headless_engine.parent.mkdir(parents=True)
    write_shell_probe(headless_engine)
    capture = tmp_path / "capture.txt"
    tools = tmp_path / "tools"
    plutil = tools / "plutil"
    tools.mkdir()
    plutil.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    make_shell_executable(plutil)
    environment = {
        **os.environ,
        "PATH": f"{shell_path(tools)}:/usr/bin:/bin",
        "SUPER_SPEECH_HOME": shell_path(runtime),
        "LAUNCHER_CAPTURE": shell_path(capture),
        "LAUNCHER_EXIT": "31",
    }

    result = run_shell_launcher(launcher, "status", environment=environment)

    assert result.returncode == 31
    assert capture.read_text(encoding="utf-8").splitlines() == [
        f"home:{shell_path(skill / 'runtime')}",
        f"models:{shell_path(skill / 'runtime' / 'models' / 'kokoro')}",
        "arg:status",
    ]


def test_shell_launcher_reports_one_installation_error(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    launcher = copy_shell_launcher(skill)
    environment = {
        **os.environ,
        "HOME": shell_path(tmp_path / "home"),
        "SUPER_SPEECH_HOME": shell_path(tmp_path / "missing"),
    }

    result = run_shell_launcher(launcher, "status", environment=environment)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == INSTALLATION_ERROR
