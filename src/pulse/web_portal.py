"""Simple web portal to trigger and monitor the Pulse GitHub workflow."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

_PORTAL_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Weekly Review Pulse</title>
  <style>
    :root {
      --bg: #050b18;
      --card: #0f1628;
      --card-border: #263753;
      --title: #f2f6ff;
      --text: #cbd6ea;
      --muted: #97a9c9;
      --accent: #2ee6ff;
      --accent-2: #8b7dff;
      --ok: #51e5b8;
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
      margin: 0;
      background:
        radial-gradient(80% 100% at 50% -10%, #1a2f57 0%, rgba(26,47,87,0) 60%),
        var(--bg);
      color: var(--text);
    }
    .wrap { max-width: 900px; margin: 46px auto; padding: 0 16px 40px; }
    .card {
      background: linear-gradient(180deg, #101a31 0%, var(--card) 100%);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 30px 30px 24px;
      box-shadow: 0 18px 45px rgba(0, 0, 0, .45), inset 0 1px 0 rgba(255,255,255,.03);
    }
    .kicker {
      color: var(--accent);
      text-transform: uppercase;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .12em;
      text-align: center;
      margin-bottom: 8px;
    }
    h1 {
      margin: 0 0 18px;
      font-size: 52px;
      letter-spacing: -.02em;
      color: var(--title);
      text-align: center;
      line-height: 1.04;
    }
    .lede {
      line-height: 1.72;
      color: var(--text);
      font-size: 21px;
      margin: 0 0 24px;
    }
    .section-title {
      margin: 26px 0 14px;
      color: var(--title);
      font-size: 30px;
      font-weight: 800;
      letter-spacing: -.01em;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .steps {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .step {
      background: rgba(255,255,255,.02);
      border: 1px solid #253553;
      border-radius: 12px;
      padding: 14px 14px 13px;
    }
    .step-head {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 21px;
      color: #f0f4ff;
      font-weight: 750;
      margin: 0 0 4px;
    }
    .step-icon {
      width: 27px;
      height: 27px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 8px;
      background: #172a44;
      color: var(--accent);
      font-size: 16px;
      flex: 0 0 27px;
    }
    .step-text { margin: 0; color: var(--muted); font-size: 18px; line-height: 1.55; }
    .cta-row { margin-top: 22px; display: flex; justify-content: center; }
    button {
      margin-top: 14px;
      background: linear-gradient(90deg, var(--accent), #22bfff);
      color: #07202b;
      border: none;
      border-radius: 10px;
      padding: 14px 24px;
      font-weight: 700;
      font-size: 18px;
      cursor: pointer;
      box-shadow: 0 8px 28px rgba(46,230,255,.3);
    }
    button[disabled] { background: #5a667e; color: #d7dbe6; cursor: not-allowed; }
    .status {
      margin-top: 12px;
      color: #9fb2d6;
      min-height: 22px;
      text-align: center;
      font-size: 14px;
    }
    .spinner {
      width: 14px;
      height: 14px;
      border: 2px solid #cfe6fb;
      border-top-color: transparent;
      border-radius: 50%;
      display: inline-block;
      vertical-align: middle;
      margin-right: 8px;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="kicker">App Reviews Insights Analyzer</div>
      <h1>Weekly Review Pulse</h1>
      <p class="lede">
        Welcome to App Reviews Insights Analyzer — your smart companion for understanding
        what users really think about your app. It reads through large volumes of app
        reviews and turns messy feedback into clear themes, trends, and key concerns.
        Instead of manually scanning thousands of comments, you get simple, actionable
        insights in minutes. Use it to spot recurring bugs, feature requests, and user
        sentiment so you can make better product decisions faster. Build what users
        actually want, improve satisfaction, and grow your app with confidence.
      </p>
      <h2 class="section-title">🧭 How It Works</h2>
      <ul class="steps">
        <li class="step">
          <h3 class="step-head"><span class="step-icon">📥</span>1. Collect</h3>
          <p class="step-text">
            We pull the latest app reviews from the App Store and Google Play automatically.
          </p>
        </li>
        <li class="step">
          <h3 class="step-head"><span class="step-icon">🧠</span>2. Understand</h3>
          <p class="step-text">
            AI reads and groups similar feedback into clear themes like bugs,
            UX issues, and feature requests.
          </p>
        </li>
        <li class="step">
          <h3 class="step-head"><span class="step-icon">✅</span>3. Validate</h3>
          <p class="step-text">
            Every insight is grounded in real user comments, so you can trust what you see.
          </p>
        </li>
        <li class="step">
          <h3 class="step-head"><span class="step-icon">📊</span>4. Summarize</h3>
          <p class="step-text">
            The platform creates a crisp weekly report with key trends,
            user quotes, and action ideas.
          </p>
        </li>
        <li class="step">
          <h3 class="step-head"><span class="step-icon">📤</span>5. Share</h3>
          <p class="step-text">
            Insights are delivered to your Google Doc and email, ready for your
            product and leadership teams.
          </p>
        </li>
      </ul>
      <div class="cta-row">
        <button id="runBtn">Get latest report</button>
      </div>
      <div id="status" class="status"></div>
    </div>
  </div>
  <script>
    const runBtn = document.getElementById("runBtn");
    const statusEl = document.getElementById("status");
    let pollTimer = null;

    function setLoading(loading) {
      runBtn.disabled = loading;
      if (loading) {
        runBtn.innerHTML = '<span class="spinner"></span>Generating report';
      } else {
        runBtn.textContent = "Get latest report";
      }
    }

    async function pollStatus(requestedAt) {
      const req = encodeURIComponent(requestedAt);
      const res = await fetch(`/api/workflow/status?requested_at=${req}`);
      const data = await res.json();
      if (!data.ok) {
        setLoading(false);
        statusEl.textContent = data.error || "Status check failed.";
        return;
      }
      if (
        data.state === "queued" ||
        data.state === "in_progress" ||
        data.state === "pending_discovery"
      ) {
        statusEl.textContent = data.phase || "Generating report…";
        return;
      }
      setLoading(false);
      if (data.state === "completed" && data.conclusion === "success") {
        statusEl.textContent = "Report generated. Opening summary page…";
        window.location.href = `/summary?requested_at=${req}`;
      } else {
        statusEl.textContent = `Workflow completed with status: ${data.conclusion || "unknown"}`;
      }
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    runBtn.addEventListener("click", async () => {
      setLoading(true);
      statusEl.textContent = "Triggering workflow…";
      try {
        const triggerRes = await fetch("/api/workflow/trigger", { method: "POST" });
        const triggerData = await triggerRes.json();
        if (!triggerData.ok) {
          setLoading(false);
          statusEl.textContent = triggerData.error || "Failed to trigger workflow.";
          return;
        }
        const requestedAt = triggerData.requested_at;
        statusEl.textContent = "Generating report…";
        pollTimer = setInterval(() => pollStatus(requestedAt), 5000);
        await pollStatus(requestedAt);
      } catch (err) {
        setLoading(false);
        statusEl.textContent = "Request failed. Check server logs.";
      }
    });
  </script>
</body>
</html>
"""

_SUMMARY_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Weekly Review Pulse Summary</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
      margin: 0;
      background: #050b18;
      color: #cbd6ea;
    }
    .wrap { max-width: 980px; margin: 24px auto 40px; padding: 0 16px; }
    .topbar { margin-bottom: 14px; }
    .back {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: #9fd9ff;
      text-decoration: none;
      font-weight: 700;
      font-size: 14px;
    }
    .status { margin: 8px 0 16px; color: #9fb2d6; min-height: 22px; }
    .summary-card {
      background: linear-gradient(180deg, #101a31 0%, #0f1628 100%);
      border: 1px solid #263753;
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 18px 45px rgba(0, 0, 0, .45);
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <a class="back" href="/">← Back</a>
    </div>
    <div id="status" class="status">Loading summary…</div>
    <div id="summary" class="summary-card"></div>
  </div>
  <script>
    const statusEl = document.getElementById("status");
    const summaryEl = document.getElementById("summary");
    const requestedAt = new URLSearchParams(window.location.search).get("requested_at");

    async function refresh() {
      if (!requestedAt) {
        statusEl.textContent = "Missing requested_at query parameter.";
        return;
      }
      const req = encodeURIComponent(requestedAt);
      const res = await fetch(`/api/workflow/status?requested_at=${req}`);
      const data = await res.json();
      if (!data.ok) {
        statusEl.textContent = data.error || "Failed to load status.";
        return;
      }
      if (
        data.state === "queued" ||
        data.state === "in_progress" ||
        data.state === "pending_discovery"
      ) {
        statusEl.textContent = data.phase || "Generating report…";
        setTimeout(refresh, 5000);
        return;
      }
      if (data.state === "completed" && data.conclusion === "success") {
        statusEl.textContent = "Report generated.";
        summaryEl.innerHTML = data.summary_html || "<p>Summary not found.</p>";
        return;
      }
      statusEl.textContent = `Workflow completed with status: ${data.conclusion || "unknown"}`;
    }

    refresh().catch(() => {
      statusEl.textContent = "Request failed. Check server logs.";
    });
  </script>
</body>
</html>
"""


def _parse_iso_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _load_summary_html() -> str:
    summary_path = "/tmp/phase4_preview/index.html"
    if not os.path.exists(summary_path):
        return (
            "<div class='card'><p>No local summary artifact found at "
            "/tmp/phase4_preview/index.html yet.</p></div>"
        )
    with open(summary_path, encoding="utf-8") as f:
        html = f.read()
    # Replace CTA with the requested post-generation text.
    html = re.sub(
        r"<div class=\"cta\">.*?</div>",
        (
            "<p class=\"lede\"><strong>"
            "The detailed report has been shared to you on your email."
            "</strong></p>"
        ),
        html,
        flags=re.S,
    )
    return html


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _trigger_workflow(repo: str, workflow_file: str, ref: str, token: str) -> None:
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    payload = {"ref": ref, "inputs": {}}
    response = requests.post(url, headers=_github_headers(token), json=payload, timeout=30)
    if response.status_code >= 300:
        raise RuntimeError(
            f"GitHub workflow dispatch failed ({response.status_code}): {response.text[:200]}"
        )


def _latest_workflow_run(
    repo: str,
    workflow_file: str,
    token: str,
    requested_at: datetime,
) -> dict[str, object] | None:
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs"
    params: dict[str, str | int] = {"event": "workflow_dispatch", "per_page": 20}
    response = requests.get(url, headers=_github_headers(token), params=params, timeout=30)
    if response.status_code >= 300:
        raise RuntimeError(
            f"GitHub workflow status failed ({response.status_code}): {response.text[:200]}"
        )
    data = response.json()
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return None
    # GitHub's run `created_at` can appear slightly earlier than our local
    # post-dispatch timestamp due to clock skew / API timing. Allow a grace window
    # so we don't get stuck in pending discovery forever after a successful trigger.
    threshold = requested_at - timedelta(minutes=5)
    for item in runs:
        if not isinstance(item, dict):
            continue
        created_at_raw = str(item.get("created_at") or "")
        if not created_at_raw:
            continue
        try:
            created_at = _parse_iso_utc(created_at_raw)
        except ValueError:
            continue
        if created_at >= threshold:
            return item
    return None


def _friendly_phase(step_name: str, status_value: str, started_at: datetime | None) -> str:
    if status_value == "queued":
        return "Queued in GitHub Actions..."
    lowered = step_name.lower()
    if "checkout" in lowered:
        return "Preparing workflow..."
    if "setup python" in lowered or "install uv" in lowered:
        return "Setting up runtime..."
    if "sync dependencies" in lowered or "cache huggingface" in lowered:
        return "Installing dependencies..."
    if "run weekly pulse" in lowered:
        if started_at is None:
            return "Running weekly pulse..."
        elapsed_s = (datetime.now(UTC) - started_at).total_seconds()
        if elapsed_s < 45:
            return "Ingesting data..."
        if elapsed_s < 90:
            return "Understanding and grouping themes..."
        if elapsed_s < 140:
            return "Rendering summary and report..."
        return "Sending email and updating Google Doc..."
    if status_value == "in_progress":
        return "Generating report..."
    return "Processing workflow..."


def _workflow_phase(
    repo: str,
    run_id: str,
    token: str,
    *,
    status_value: str,
    started_at: datetime | None,
) -> str:
    if status_value == "queued":
        return "Queued in GitHub Actions..."
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs"
    response = requests.get(url, headers=_github_headers(token), timeout=30)
    if response.status_code >= 300:
        return "Generating report..."
    payload = response.json()
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return "Generating report..."
    for job in jobs:
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            if str(step.get("status") or "") == "in_progress":
                name = str(step.get("name") or "")
                return _friendly_phase(name, status_value, started_at)
    return _friendly_phase("", status_value, started_at)


def serve_portal(host: str = "127.0.0.1", port: int = 8780) -> None:
    repo = os.environ.get("PULSE_GITHUB_REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    token = os.environ.get("GITHUB_TOKEN") or ""
    workflow_file = os.environ.get("PULSE_WORKFLOW_FILE") or "pulse.yml"
    ref = os.environ.get("PULSE_WORKFLOW_REF") or "main"

    class PortalHandler(BaseHTTPRequestHandler):
        def _json(self, payload: dict[str, object], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, html: str, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._html(_PORTAL_HTML)
                return
            if parsed.path == "/summary":
                self._html(_SUMMARY_PAGE_HTML)
                return
            if parsed.path == "/api/workflow/status":
                if not repo or not token:
                    self._json(
                        {
                            "ok": False,
                            "error": "Missing GITHUB_TOKEN or PULSE_GITHUB_REPO/GITHUB_REPOSITORY.",
                        },
                        status=400,
                    )
                    return
                q = parse_qs(parsed.query)
                requested_at_raw = str((q.get("requested_at") or [""])[0])
                if not requested_at_raw:
                    self._json({"ok": False, "error": "requested_at is required."}, status=400)
                    return
                try:
                    requested_at = _parse_iso_utc(requested_at_raw)
                except ValueError:
                    self._json({"ok": False, "error": "Invalid requested_at format."}, status=400)
                    return
                try:
                    run = _latest_workflow_run(repo, workflow_file, token, requested_at)
                except RuntimeError as exc:
                    self._json({"ok": False, "error": str(exc)}, status=502)
                    return
                if run is None:
                    self._json({"ok": True, "state": "pending_discovery"})
                    return
                status_value = str(run.get("status") or "")
                conclusion = str(run.get("conclusion") or "")
                run_id = str(run.get("id") or "")
                started_at_raw = str(run.get("run_started_at") or "")
                started_at: datetime | None = None
                if started_at_raw:
                    try:
                        started_at = _parse_iso_utc(started_at_raw)
                    except ValueError:
                        started_at = None
                payload: dict[str, object] = {
                    "ok": True,
                    "state": "completed" if status_value == "completed" else status_value,
                    "conclusion": conclusion,
                    "run_id": run.get("id"),
                    "run_url": run.get("html_url"),
                }
                if run_id:
                    payload["phase"] = _workflow_phase(
                        repo,
                        run_id,
                        token,
                        status_value=status_value,
                        started_at=started_at,
                    )
                if status_value == "completed" and conclusion == "success":
                    payload["summary_html"] = _load_summary_html()
                self._json(payload)
                return
            self.send_error(404, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/workflow/trigger":
                self.send_error(404, "Not found")
                return
            if not repo or not token:
                self._json(
                    {
                        "ok": False,
                        "error": "Missing GITHUB_TOKEN or PULSE_GITHUB_REPO/GITHUB_REPOSITORY.",
                    },
                    status=400,
                )
                return
            try:
                _trigger_workflow(repo, workflow_file, ref, token)
            except RuntimeError as exc:
                self._json({"ok": False, "error": str(exc)}, status=502)
                return
            self._json({"ok": True, "requested_at": datetime.now(UTC).isoformat()})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((host, port), PortalHandler)
    server.serve_forever()


__all__ = ["serve_portal"]
