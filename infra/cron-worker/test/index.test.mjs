// SYNTHETIC FIXTURE: fake fetch responses and a fake token; no network.
// Run with `node --test infra/cron-worker/test/` (tests/unit/test_cron_worker.py runs it too
// when node is installed).
import assert from "node:assert/strict";
import { test } from "node:test";

import { cronMatches, dispatch, due, slotOf, tick } from "../src/index.js";

const ms = (iso) => Date.parse(iso);
const ENV = { GITHUB_REPO: "owner/repo", GITHUB_REF: "main", GITHUB_TOKEN: "fake-token-123" };

function recorder() {
  const lines = [];
  return { lines, log: (s) => lines.push(s), error: (s) => lines.push(s) };
}

function fakeFetch(statuses) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    const status = statuses.shift();
    if (status === "throw") throw new TypeError("network down");
    return { ok: status >= 200 && status < 300, status };
  };
  return { calls, impl };
}

test("slot is the scheduled minute, not when the code ran", () => {
  assert.equal(slotOf(ms("2026-11-17T13:15:00.000Z")), "2026-11-17T13:15:00Z");
  assert.equal(slotOf(ms("2026-11-17T13:15:42.917Z")), "2026-11-17T13:15:00Z");
});

test("due entries per minute", () => {
  const at = (iso) => due(ms(iso)).map((e) => `${e.workflow} ${e.cron} ${e.slot}`);
  assert.deepEqual(at("2026-07-15T12:15:00Z"), [
    "intraday.yml 15 12 * * 1-5 2026-07-15T12:15:00Z",
  ]);
  assert.deepEqual(at("2026-11-17T13:15:00Z"), [
    "intraday.yml */15 13-21 * * 1-5 2026-11-17T13:15:00Z",
  ]);
  assert.deepEqual(at("2026-11-17T10:15:00Z"), ["morning.yml 15 10 * * 1-5 2026-11-17T10:15:00Z"]);
  assert.deepEqual(at("2026-11-17T22:30:00Z"), ["eod.yml 30 22 * * 1-5 2026-11-17T22:30:00Z"]);
  assert.deepEqual(at("2026-11-17T12:20:00Z"), ["sla.yml 20 12 * * 1-5 2026-11-17T12:20:00Z"]);
  assert.deepEqual(at("2026-11-17T13:16:00Z"), []);
  assert.deepEqual(at("2026-11-21T13:15:00Z"), []); // Saturday
  assert.deepEqual(at("2026-11-17T22:00:00Z"), []); // after the last */15 hour
});

test("cron matching: ranges, steps, lists and day-of-week", () => {
  const d = new Date("2026-11-22T00:00:00Z"); // Sunday
  assert.equal(cronMatches("0 0 * * 0", d), true);
  assert.equal(cronMatches("0 0 * * 7", d), true);
  assert.equal(cronMatches("0 0 * * 1-5", d), false);
  assert.equal(cronMatches("0,30 0 22 11 *", d), true);
  assert.equal(cronMatches("0 0 1 * 0", d), true); // both restricted: either matches
  assert.throws(() => cronMatches("0 0 * *", d));
  assert.throws(() => cronMatches("61 0 * * *", d));
});

test("dispatch posts fire and slot and logs the status, never the token", async () => {
  const f = fakeFetch([204]);
  const rec = recorder();
  const entry = { workflow: "intraday.yml", cron: "15 12 * * 1-5", slot: "2026-07-15T12:15:00Z" };
  assert.equal(await dispatch(entry, ENV, f.impl, rec), true);
  assert.equal(f.calls.length, 1);
  assert.equal(
    f.calls[0].url,
    "https://api.github.com/repos/owner/repo/actions/workflows/intraday.yml/dispatches",
  );
  assert.deepEqual(JSON.parse(f.calls[0].init.body), {
    ref: "main",
    inputs: { fire: "15 12 * * 1-5", slot: "2026-07-15T12:15:00Z" },
  });
  assert.equal(f.calls[0].init.headers.Authorization, "Bearer fake-token-123");
  assert.equal(rec.lines.length, 1);
  assert.match(rec.lines[0], /"status":204/);
  assert.ok(rec.lines.every((l) => !l.includes("fake-token-123")));
});

test("a non-2xx dispatch is retried once, then reported", async () => {
  const f = fakeFetch([502, 204]);
  const rec = recorder();
  const entry = { workflow: "eod.yml", cron: "30 22 * * 1-5", slot: "2026-11-17T22:30:00Z" };
  assert.equal(await dispatch(entry, ENV, f.impl, rec), true);
  assert.equal(f.calls.length, 2);
  assert.match(rec.lines[0], /dispatch.failed.*"status":502/);

  const g = fakeFetch([401, 401, 204]);
  assert.equal(await dispatch(entry, ENV, g.impl, rec), false);
  assert.equal(g.calls.length, 2); // once more, not forever
  assert.ok(rec.lines.every((l) => !l.includes("fake-token-123")));
});

test("a network error is retried once and logged without the token", async () => {
  const f = fakeFetch(["throw", "throw"]);
  const rec = recorder();
  const entry = { workflow: "sla.yml", cron: "20 12 * * 1-5", slot: "2026-11-17T12:20:00Z" };
  assert.equal(await dispatch(entry, ENV, f.impl, rec), false);
  assert.equal(f.calls.length, 2);
  assert.ok(rec.lines.every((l) => l.includes("dispatch.error") && !l.includes("fake-token")));
});

test("tick dispatches every due entry and nothing on a quiet minute", async () => {
  const f = fakeFetch([204]);
  const rec = recorder();
  assert.deepEqual(await tick(ms("2026-11-17T22:30:07Z"), ENV, f.impl, rec), [true]);
  const g = fakeFetch([]);
  assert.deepEqual(await tick(ms("2026-11-17T22:31:00Z"), ENV, g.impl, rec), []);
  assert.equal(g.calls.length, 0);
});

test("a fire that still fails after its retry fails the tick with a summary", async () => {
  const f = fakeFetch([500, 503]);
  const rec = recorder();
  await assert.rejects(
    () => tick(ms("2026-11-17T22:30:00Z"), ENV, f.impl, rec),
    (err) => {
      assert.match(err.message, /1 of 1 dispatches failed: eod.yml/);
      assert.ok(!err.message.includes("fake-token-123"));
      return true;
    },
  );
  assert.equal(f.calls.length, 2);
  const summary = rec.lines.filter((l) => l.includes("tick.failed"));
  assert.equal(summary.length, 1);
  assert.deepEqual(JSON.parse(summary[0]).failed, [
    { workflow: "eod.yml", fire: "30 22 * * 1-5", slot: "2026-11-17T22:30:00Z" },
  ]);
  assert.ok(rec.lines.every((l) => !l.includes("fake-token-123")));
});

test("the token appears in no log line on the ok, failure and error paths", async () => {
  const rec = recorder();
  const entry = { workflow: "eod.yml", cron: "30 22 * * 1-5", slot: "2026-11-17T22:30:00Z" };
  await dispatch(entry, ENV, fakeFetch([204]).impl, rec);
  await dispatch(entry, ENV, fakeFetch([403, 401]).impl, rec);
  await dispatch(entry, ENV, fakeFetch(["throw", "throw"]).impl, rec);
  await tick(ms("2026-11-17T22:30:00Z"), ENV, fakeFetch(["throw", 500]).impl, rec).catch(() => {});
  const events = rec.lines.map((l) => JSON.parse(l).event);
  for (const e of ["dispatch.ok", "dispatch.failed", "dispatch.error", "tick.failed"]) {
    assert.ok(events.includes(e), e);
  }
  for (const line of rec.lines) {
    assert.ok(!line.includes("fake-token-123"), line);
    assert.ok(!line.includes("Bearer"), line);
  }
});
