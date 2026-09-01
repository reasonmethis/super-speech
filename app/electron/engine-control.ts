import { readFileSync } from "node:fs";
import { request } from "node:http";
import path from "node:path";

const CONTROL_ENDPOINT_FILENAME = "control.json";
const CONTROL_PROTOCOL_VERSION = 1;
const MAX_RESPONSE_BYTES = 64 * 1024 * 1024;

interface EngineControlEndpoint {
  version: typeof CONTROL_PROTOCOL_VERSION;
  engine_pid: number;
  port: number;
  token: string;
}

export interface PlaybackControlAck {
  version: typeof CONTROL_PROTOCOL_VERSION;
  engine_pid: number;
  state: "idle" | "paused" | "playing";
  updated_at: number;
  audio_state: "idle" | "paused" | "playing";
}

function isPlaybackState(
  value: unknown,
): value is PlaybackControlAck["state"] {
  return value === "idle" || value === "paused" || value === "playing";
}

export function parseEngineControlEndpoint(
  value: unknown,
  enginePid: number,
): EngineControlEndpoint | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const endpoint = value as Record<string, unknown>;
  const port = endpoint.port;
  const token = endpoint.token;
  if (
    Object.keys(endpoint).length !== 4 ||
    endpoint.version !== CONTROL_PROTOCOL_VERSION ||
    endpoint.engine_pid !== enginePid ||
    typeof port !== "number" ||
    !Number.isInteger(port) ||
    port <= 0 ||
    port > 65_535 ||
    typeof token !== "string" ||
    !/^[0-9a-f]{64}$/.test(token)
  ) {
    return null;
  }
  return {
    version: CONTROL_PROTOCOL_VERSION,
    engine_pid: enginePid,
    port,
    token,
  };
}

function readEngineControlEndpoint(
  runtimeDirectory: string,
  enginePid: number,
): EngineControlEndpoint {
  let value: unknown;
  try {
    value = JSON.parse(
      readFileSync(path.join(runtimeDirectory, CONTROL_ENDPOINT_FILENAME), "utf8"),
    );
  } catch {
    throw new Error("The running Super Speech engine has no control endpoint");
  }
  const endpoint = parseEngineControlEndpoint(value, enginePid);
  if (!endpoint) {
    throw new Error("The Super Speech engine control endpoint is incompatible");
  }
  return endpoint;
}

export function parsePlaybackControlAck(
  value: unknown,
  enginePid: number,
): PlaybackControlAck | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const ack = value as Record<string, unknown>;
  if (
    Object.keys(ack).length !== 5 ||
    ack.version !== 1 ||
    ack.engine_pid !== enginePid ||
    !isPlaybackState(ack.state) ||
    typeof ack.updated_at !== "number" ||
    !Number.isFinite(ack.updated_at) ||
    ack.updated_at <= 0 ||
    !isPlaybackState(ack.audio_state)
  ) {
    return null;
  }
  return {
    version: CONTROL_PROTOCOL_VERSION,
    engine_pid: enginePid,
    state: ack.state,
    updated_at: ack.updated_at,
    audio_state: ack.audio_state,
  };
}

export function runEngineControl(
  runtimeDirectory: string,
  enginePid: number,
  payload: object,
  timeoutMs = 65_000,
): Promise<unknown> {
  const endpoint = readEngineControlEndpoint(runtimeDirectory, enginePid);
  const body = JSON.stringify(payload);
  return new Promise((resolve, reject) => {
    const controlRequest = request(
      {
        host: "127.0.0.1",
        port: endpoint.port,
        path: "/v1/control",
        method: "POST",
        headers: {
          Authorization: `Bearer ${endpoint.token}`,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
          Connection: "close",
        },
      },
      (response) => {
        response.setEncoding("utf8");
        let responseBody = "";
        let responseBytes = 0;
        response.on("data", (chunk: string) => {
          responseBody += chunk;
          responseBytes += Buffer.byteLength(chunk);
          if (responseBytes > MAX_RESPONSE_BYTES) {
            controlRequest.destroy(new Error("Engine control response is too large"));
          }
        });
        response.on("end", () => {
          let parsed: unknown;
          try {
            parsed = JSON.parse(responseBody);
          } catch {
            reject(new Error("Engine control returned invalid JSON"));
            return;
          }
          const envelope = parsed && typeof parsed === "object" && !Array.isArray(parsed)
            ? parsed as Record<string, unknown>
            : null;
          if (
            response.statusCode === 200 &&
            envelope &&
            Object.hasOwn(envelope, "result")
          ) {
            resolve(envelope.result);
            return;
          }
          const error = envelope && typeof envelope.error === "string"
            ? envelope.error
            : `Engine control failed with HTTP ${response.statusCode ?? "unknown"}`;
          reject(new Error(error));
        });
      },
    );
    controlRequest.setTimeout(timeoutMs, () => {
      controlRequest.destroy(new Error("Engine control timed out"));
    });
    controlRequest.once("error", reject);
    controlRequest.end(body);
  });
}
