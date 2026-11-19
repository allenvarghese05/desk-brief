// Desk Brief cron Worker (ruling P-018, card T43).
//
// One Cron Trigger fires every minute. On each tick the Worker checks the schedule table below
// (UTC, the same crons as the four workflows' `schedule:` blocks) and, for every entry whose cron
// matches the tick's scheduled minute, calls GitHub's workflow_dispatch with two inputs:
//   fire: the exact cron string (the workflow maps it into GITHUB_EVENT_SCHEDULE)
//   slot: the scheduled minute in UTC, ISO 8601 (the workflow maps it into DESKBRIEF_SLOT)
// The slot comes from the tick's scheduledTime and the table, never from when the code ran, so
// a tick that starts a few seconds late still names the minute the run belongs to.
//
// Secrets: GITHUB_TOKEN is a fine-grained PAT (this repository, Actions: write) set with
// `wrangler secret put GITHUB_TOKEN`. It is never logged.

// The table below is parsed as JSON by tests/unit/test_cron_worker.py, which fails if it differs
// from the workflows' crons. Keep it a JSON array: double quotes, no trailing commas.
export const SCHEDULE = [
  { "workflow": "morning.yml", "cron": "15 10 * * 1-5" },
  { "workflow": "morning.yml", "cron": "15 11 * * 1-5" },
  { "workflow": "intraday.yml", "cron": "*/15 13-21 * * 1-5" },
  { "workflow": "intraday.yml", "cron": "15 12 * * 1-5" },
  { "workflow": "eod.yml", "cron": "30 22 * * 1-5" },
  { "workflow": "sla.yml", "cron": "20 12 * * 1-5" }
];

const RANGES = [
  [0, 59], // minute
  [0, 23], // hour
  [1, 31], // day of month
  [1, 12], // month
  [0, 6], // day of week, 0 = Sunday (7 is accepted as Sunday too)
];

function field(spec, lo, hi) {
  const out = new Set();
  for (const part of spec.split(",")) {
    const [rng, stepStr] = part.split("/");
    const step = stepStr === undefined ? 1 : Number(stepStr);
    if (!Number.isInteger(step) || step < 1) throw new Error(`bad cron step in ${spec}`);
    let start;
    let end;
    if (rng === "*") {
      [start, end] = [lo, hi];
    } else if (rng.includes("-")) {
      [start, end] = rng.split("-").map(Number);
    } else {
      start = Number(rng);
      end = stepStr === undefined ? start : hi;
    }
    const sundaySeven = hi === 6 && end === 7;
    if (!(Number.isInteger(start) && Number.isInteger(end))) throw new Error(`bad cron ${spec}`);
    if (!(lo <= start && start <= end && (end <= hi || sundaySeven))) {
      throw new Error(`cron value out of range in ${spec}`);
    }
    for (let v = start; v <= end; v += step) out.add(hi === 6 ? v % 7 : v);
  }
  return out;
}

/** Whether the 5-field cron `expr` matches the UTC minute of `date`. */
export function cronMatches(expr, date) {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) throw new Error(`expected 5 cron fields, got ${expr}`);
  const [minute, hour, dom, month, dow] = parts.map((p, i) => field(p, ...RANGES[i]));
  const domAny = parts[2] === "*";
  const dowAny = parts[4] === "*";
  const dayOk =
    domAny || dowAny
      ? dom.has(date.getUTCDate()) && dow.has(date.getUTCDay())
      : dom.has(date.getUTCDate()) || dow.has(date.getUTCDay()); // standard cron: either
  return (
    minute.has(date.getUTCMinutes()) &&
    hour.has(date.getUTCHours()) &&
    month.has(date.getUTCMonth() + 1) &&
    dayOk
  );
}

/** `scheduledTime` (ms) floored to its minute, as `YYYY-MM-DDTHH:MM:00Z`. */
export function slotOf(scheduledTime) {
  const d = new Date(Math.floor(scheduledTime / 60000) * 60000);
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Table entries due at `scheduledTime`, each with its slot. */
export function due(scheduledTime, table = SCHEDULE) {
  const slot = slotOf(scheduledTime);
  const at = new Date(slot);
  return table.filter((e) => cronMatches(e.cron, at)).map((e) => ({ ...e, slot }));
}

async function post(entry, env, fetchImpl) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${entry.workflow}/dispatches`;
  return fetchImpl(url, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "desk-brief-cron-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      ref: env.GITHUB_REF || "main",
      inputs: { fire: entry.cron, slot: entry.slot },
    }),
  });
}

/**
 * Dispatch one entry; a non-2xx answer or a network error is retried once.
 * Logs the workflow, fire, slot, attempt and HTTP status. Never the token or the request headers.
 */
export async function dispatch(entry, env, fetchImpl = fetch, log = console) {
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const base = { workflow: entry.workflow, fire: entry.cron, slot: entry.slot, attempt };
    try {
      const res = await post(entry, env, fetchImpl);
      const record = { ...base, status: res.status, dispatched_at: new Date().toISOString() };
      if (res.ok) {
        log.log(JSON.stringify({ event: "dispatch.ok", ...record }));
        return true;
      }
      log.error(JSON.stringify({ event: "dispatch.failed", ...record }));
    } catch (err) {
      log.error(JSON.stringify({ event: "dispatch.error", ...base, error: String(err?.name) }));
    }
  }
  return false;
}

export async function tick(scheduledTime, env, fetchImpl = fetch, log = console) {
  const entries = due(scheduledTime);
  return Promise.all(entries.map((e) => dispatch(e, env, fetchImpl, log)));
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(tick(controller.scheduledTime, env));
  },
};
