import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import {
  parseEngineControlEndpoint,
  parsePlaybackControlAck,
  runEngineControl,
} from "./engine-control.ts";

test("accepts only the endpoint owned by the current engine process", () => {
  const endpoint = {
    version: 1,
    engine_pid: 123,
    port: 4567,
    token: "a".repeat(64),
  };

  assert.deepEqual(parseEngineControlEndpoint(endpoint, 123), endpoint);
  assert.equal(parseEngineControlEndpoint(endpoint, 456), null);
  assert.equal(
    parseEngineControlEndpoint({ ...endpoint, token: "unsafe" }, 123),
    null,
  );
});

test("accepts only playback acknowledgements from the current engine", () => {
  const ack = {
    version: 1,
    engine_pid: 123,
    state: "paused",
    updated_at: 100,
    audio_state: "paused",
  };

  assert.deepEqual(parsePlaybackControlAck(ack, 123), ack);
  assert.equal(parsePlaybackControlAck(ack, 456), null);
  assert.equal(parsePlaybackControlAck({ ...ack, state: "loading" }, 123), null);
});

test("sends authenticated commands to the running engine endpoint", async () => {
  const runtime = await mkdtemp(path.join(tmpdir(), "super-speech-control-"));
  const token = "b".repeat(64);
  const requests: unknown[] = [];
  const server = createServer((request, response) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      assert.equal(request.url, "/v1/control");
      assert.equal(request.headers.authorization, `Bearer ${token}`);
      requests.push(JSON.parse(body));
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ result: { state: "paused" } }));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const address = server.address();
    assert(address && typeof address === "object");
    await writeFile(
      path.join(runtime, "control.json"),
      JSON.stringify({
        version: 1,
        engine_pid: 123,
        port: address.port,
        token,
      }),
    );

    assert.deepEqual(
      await runEngineControl(runtime, 123, { command: "pause" }),
      { state: "paused" },
    );
    assert.deepEqual(requests, [{ command: "pause" }]);
  } finally {
    await new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
    });
    await rm(runtime, { recursive: true, force: true });
  }
});
