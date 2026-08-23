import { spawnSync } from "node:child_process";
import process from "node:process";


const configuredPython = process.env.SUPER_SPEECH_BUILD_PYTHON;
const python = configuredPython ?? (process.platform === "win32" ? "py" : "python3");
const versionArgs = !configuredPython && process.platform === "win32" ? ["-3.12"] : [];

function runPython(args) {
  const result = spawnSync(python, [...versionArgs, ...args], {
    cwd: new URL("..", import.meta.url),
    stdio: "inherit",
    shell: false,
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

runPython(["-m", "pip", "install", "-r", "../requirements-build.txt"]);
runPython(["scripts/stage_models.py"]);
runPython(["scripts/build_engine.py"]);
