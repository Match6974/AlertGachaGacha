import assert from "node:assert/strict";
import { afterEach, mock, test } from "node:test";
import { readFile } from "node:fs/promises";
import worker, { dispatchWorkflow, DISPATCH_URL } from "../src/index.js";

const TOKEN = "github_pat_test_value_never_log";
const env = { GITHUB_TOKEN: TOKEN };

function logger() {
  return { log: mock.fn(), error: mock.fn() };
}

function logged(log) {
  return JSON.stringify([...log.log.mock.calls, ...log.error.mock.calls]);
}

afterEach(() => mock.restoreAll());

test("dispatches the exact workflow and main branch with dry_run false", async () => {
  const fetchImpl = mock.fn(async () => new Response(null, { status: 204 }));
  const log = logger();
  await dispatchWorkflow(env, { fetchImpl, logger: log });
  assert.equal(fetchImpl.mock.callCount(), 1);
  const [url, request] = fetchImpl.mock.calls[0].arguments;
  assert.equal(url, DISPATCH_URL);
  assert.equal(url, "https://api.github.com/repos/Match6974/AlertGachaGacha/actions/workflows/monitor.yml/dispatches");
  assert.equal(request.method, "POST");
  assert.equal(request.redirect, "error");
  assert.equal(request.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(request.headers["X-GitHub-Api-Version"], "2022-11-28");
  assert.ok(request.headers["User-Agent"]);
  assert.deepEqual(JSON.parse(request.body), { ref: "main", inputs: { dry_run: false } });
  assert.match(logged(log), /github_dispatch_accepted/);
  assert.match(logged(log), /not checked; acceptance is not workflow success/);
  assert.ok(!logged(log).includes(TOKEN));
});

test("missing, blank or invalid Secret fails before any network call", async () => {
  const fetchImpl = mock.fn();
  for (const value of [undefined, "", "  ", "token\nwith-newline"]) {
    const log = logger();
    await assert.rejects(dispatchWorkflow({ GITHUB_TOKEN: value }, { fetchImpl, logger: log }), /Secret GITHUB_TOKEN/);
  }
  assert.equal(fetchImpl.mock.callCount(), 0);
});

for (const status of [401, 403, 404, 422, 429, 500]) {
  test(`HTTP ${status} logs details and fails without retry`, async () => {
    const log = logger();
    const fetchImpl = mock.fn(async () => new Response(JSON.stringify({ message: "API error", echoed: TOKEN }), {
      status,
      headers: { "x-github-request-id": "test-request-id", "retry-after": "60" },
    }));
    await assert.rejects(dispatchWorkflow(env, { fetchImpl, logger: log }), new RegExp(`HTTP ${status}`));
    assert.equal(fetchImpl.mock.callCount(), 1);
    const output = logged(log);
    assert.match(output, /API error/);
    assert.match(output, /test-request-id/);
    assert.match(output, /REDACTED/);
    assert.ok(!output.includes(TOKEN));
    assert.ok(!output.includes("github_dispatch_accepted"));
  });
}

test("transport failure redacts the error and never retries", async () => {
  const log = logger();
  const fetchImpl = mock.fn(async () => { throw new Error(`Connection failed ${TOKEN}`); });
  await assert.rejects(dispatchWorkflow(env, { fetchImpl, logger: log }), /acceptance is unknown/);
  assert.equal(fetchImpl.mock.callCount(), 1);
  assert.match(logged(log), /Connection failed/);
  assert.ok(!logged(log).includes(TOKEN));
});

test("a stalled request times out and fails without retry", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const log = logger();
  const fetchImpl = mock.fn((_url, request) => new Promise((_resolve, reject) => {
    request.signal.addEventListener("abort", () => reject(new Error("aborted")));
  }));
  const result = dispatchWorkflow(env, { fetchImpl, logger: log });
  t.mock.timers.tick(30_000);
  await assert.rejects(result, /transport failed/);
  assert.equal(fetchImpl.mock.callCount(), 1);
  assert.match(logged(log), /"timed_out":true/);
});

test("an error body that cannot be read still fails with the HTTP status", async () => {
  const log = logger();
  const body = new ReadableStream({ start(controller) { controller.error(new Error("unreadable")); } });
  await assert.rejects(dispatchWorkflow(env, {
    fetchImpl: async () => new Response(body, { status: 502 }), logger: log,
  }), /HTTP 502/);
  assert.match(logged(log), /Could not read GitHub error response/);
});

test("error response details have a bounded size", async () => {
  const log = logger();
  await assert.rejects(dispatchWorkflow(env, {
    fetchImpl: async () => new Response("x".repeat(20_000), { status: 500 }), logger: log,
  }));
  const details = log.error.mock.calls[0].arguments[1].detail;
  assert.equal(details.length, 8192);
});

test("scheduled handler awaits dispatch and propagates errors", async () => {
  const fetchMock = mock.method(globalThis, "fetch", async () => new Response(null, { status: 204 }));
  const event = { cron: "*/15 * * * *", scheduledTime: 1_700_000_000_000 };
  await worker.scheduled(event, env, {});
  assert.equal(fetchMock.mock.callCount(), 1);
  await assert.rejects(worker.scheduled(event, {}, {}), /Secret GITHUB_TOKEN/);
});

test("all HTTP requests return 404 without calling GitHub", async () => {
  const fetchMock = mock.method(globalThis, "fetch", () => { throw new Error("Must never call GitHub"); });
  for (const method of ["GET", "POST"]) {
    for (const path of ["/", "/__scheduled", "/dispatch"]) {
      const response = await worker.fetch(new Request(`https://example.invalid${path}`, { method }), env, {});
      assert.equal(response.status, 404);
    }
  }
  assert.equal(fetchMock.mock.callCount(), 0);
});

test("configuration schedules every 15 minutes and disables public URLs", async () => {
  const config = JSON.parse(await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8"));
  assert.deepEqual(config.triggers.crons, ["*/15 * * * *"]);
  assert.equal(config.workers_dev, false);
  assert.equal(config.preview_urls, false);
  assert.equal(config.main, "src/index.js");
  assert.equal(config.vars?.GITHUB_TOKEN, undefined);
});
