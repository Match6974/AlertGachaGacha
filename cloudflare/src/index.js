export const DISPATCH_URL =
  "https://api.github.com/repos/Match6974/AlertGachaGacha/actions/workflows/monitor.yml/dispatches";

const TIMEOUT_MS = 30_000;
const MAX_ERROR_BYTES = 8192;

function redact(value, token) {
  let text = String(value);
  for (const secret of [token, encodeURIComponent(token)]) {
    if (secret) text = text.replaceAll(secret, "[REDACTED]");
  }
  // Also mask recognizable PATs if an oversized response truncates a token.
  return text.replace(/\b(?:github_pat|gh[pousr])_[A-Za-z0-9_]+/g, "[REDACTED]");
}

async function errorDetails(response) {
  if (!response.body) return "(empty response)";
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (size < MAX_ERROR_BYTES) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value.subarray(0, MAX_ERROR_BYTES - size);
      chunks.push(chunk);
      size += chunk.length;
    }
  } finally {
    await reader.cancel();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return new TextDecoder().decode(bytes) || "(empty response)";
}

export async function dispatchWorkflow(env, { fetchImpl = fetch, logger = console } = {}) {
  const token = typeof env.GITHUB_TOKEN === "string" ? env.GITHUB_TOKEN.trim() : "";
  if (!token || /\s/.test(token)) {
    logger.error("dispatch_configuration_error", { message: "Secret GITHUB_TOKEN is missing or invalid." });
    throw new Error("Secret GITHUB_TOKEN is missing or invalid.");
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    let response;
    try {
      response = await fetchImpl(DISPATCH_URL, {
        method: "POST",
        // Workers supports follow/manual; never forward the PAT to a redirect.
        redirect: "manual",
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "User-Agent": "AlertGachaGacha-Cloudflare-Scheduler",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main", inputs: { dry_run: false } }),
        signal: controller.signal,
      });
    } catch (error) {
      // Never log the raw error object: it may contain request headers.
      logger.error("github_dispatch_transport_error", {
        message: redact(error instanceof Error ? error.message : "Network request failed", token),
        timed_out: controller.signal.aborted,
        acceptance: "unknown; no automatic retry",
      });
      throw new Error("GitHub dispatch transport failed; request acceptance is unknown.");
    }

    logger.log("github_dispatch_response", { status: response.status });
    if (response.status !== 204) {
      let detail;
      try {
        detail = await errorDetails(response);
      } catch {
        detail = "Could not read GitHub error response.";
      }
      logger.error("github_dispatch_rejected", {
        status: response.status,
        request_id: redact(response.headers.get("x-github-request-id") || "", token),
        retry_after: redact(response.headers.get("retry-after") || "", token),
        detail: redact(detail, token),
      });
      throw new Error(`GitHub dispatch was not accepted (HTTP ${response.status}).`);
    }

    logger.log("github_dispatch_accepted", {
      status: 204,
      ref: "main",
      workflow: "monitor.yml",
      workflow_result: "not checked; acceptance is not workflow success",
    });
  } finally {
    clearTimeout(timeout);
  }
}

export default {
  async scheduled(controller, env, _ctx) {
    console.log("cron_trigger", { cron: controller.cron, scheduled_time: controller.scheduledTime });
    // Await so a rejection marks this scheduled invocation as failed.
    await dispatchWorkflow(env);
  },

  fetch() {
    // HTTP requests can never dispatch a workflow, even with a valid Secret.
    return new Response("Not Found", { status: 404 });
  },
};
