"""Production-ready Flask backend for the Test Case Generator web app.

The service accepts high-level project details, builds a lightweight test plan,
tracks generation progress, and exposes download endpoints for the resulting
report. Everything is self contained—no external APIs or cloud credentials are
required—so the app can run locally or in production with minimal setup.
"""
from __future__ import annotations

import itertools
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import Dict, List

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import openpyxl
from openpyxl.styles import Alignment, Font

app = Flask(__name__)
app.config["SECRET_KEY"] = "safe-testcase-secret"
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ScreenInput:
    name: str
    platform: str
    goals: List[str]


@dataclass
class TestCase:
    case_id: str
    title: str
    priority: str
    category: str
    steps: List[str]
    expected: str


@dataclass
class GenerationResult:
    session_id: str
    project_name: str
    created_at: str
    summary: Dict[str, str]
    screens: List[Dict]
    recommendations: Dict[str, List[str]]


@dataclass
class JobState:
    session_id: str
    status: str = "pending"  # pending|running|complete|error
    progress: int = 0
    messages: List[Dict[str, str]] = field(default_factory=list)
    result: GenerationResult | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# In-memory job store
# ---------------------------------------------------------------------------

jobs: Dict[str, JobState] = {}
jobs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def log_job(job: JobState, message: str, level: str = "info") -> None:
    job.messages.append(
        {
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
    )


def validate_payload(payload: Dict) -> Dict:
    """Validate and normalize incoming generation requests."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid payload")

    project_name = (payload.get("projectName") or "").strip()
    description = (payload.get("description") or "").strip()
    release = (payload.get("release") or "Q1")[:10]
    owner = (payload.get("owner") or "Unknown").strip() or "Unknown"

    if not project_name:
        raise ValueError("Project name is required")

    screens_payload = payload.get("screens") or []
    screens: List[ScreenInput] = []
    for screen in screens_payload:
        name = (screen.get("name") or "").strip()
        platform = (screen.get("platform") or "WEB").upper()
        goals = [g.strip() for g in screen.get("goals", []) if g.strip()]
        if not name:
            raise ValueError("Each screen requires a name")
        if platform not in {"WEB", "IOS", "ANDROID", "TABLET"}:
            platform = "WEB"
        if not goals:
            goals = ["Validate core interactions", "Check visual polish"]
        screens.append(ScreenInput(name=name, platform=platform, goals=goals))

    if not screens:
        screens.append(
            ScreenInput(
                name="Landing Page",
                platform="WEB",
                goals=["Validate primary hero CTA", "Check accessibility basics"],
            )
        )

    return {
        "project_name": project_name,
        "description": description,
        "release": release,
        "owner": owner,
        "screens": screens,
    }


# ---------------------------------------------------------------------------
# Generation engine
# ---------------------------------------------------------------------------

PRIORITY_ORDER = ["Critical", "High", "Medium", "Low"]
CATEGORIES = [
    "Navigation",
    "Forms",
    "Accessibility",
    "Performance",
    "Visual QA",
    "Error handling",
    "Security",
]


def build_test_cases(screen: ScreenInput, session_id: str) -> List[TestCase]:
    """Create deterministic yet varied test cases for a screen."""
    seed = int(uuid.UUID(session_id)) % 10_000
    randomizer = itertools.cycle(range(len(PRIORITY_ORDER)))
    cases: List[TestCase] = []

    templates = [
        ("Navigation", f"{screen.name}: primary path works", [
            "Load the screen",
            "Trigger the main call-to-action",
            "Confirm user lands on the expected destination",
        ], "Target destination is correct and UI keeps context"),
        ("Forms", f"{screen.name}: form validation", [
            "Attempt to submit with empty required fields",
            "Provide invalid formats (email, phone) where applicable",
            "Submit with valid data",
        ], "Validation errors show inline; success submits once"),
        ("Accessibility", f"{screen.name}: keyboard & contrast", [
            "Navigate via keyboard only",
            "Check focus order and visible states",
            "Inspect contrast for primary text and buttons",
        ], "All focusable items are reachable; contrast meets WCAG AA"),
        ("Error handling", f"{screen.name}: failure states", [
            "Force the primary API call to fail (mock or offline mode)",
            "Observe error messaging and retry options",
        ], "Clear, actionable error is shown and retry is offered"),
        ("Performance", f"{screen.name}: first meaningful paint", [
            "Load over a throttled connection",
            "Measure time to interactive and content shifts",
        ], "Layout remains stable; page is interactive under 3s"),
    ]

    for idx, (category, title, steps, expected) in enumerate(templates, start=1):
        priority = PRIORITY_ORDER[(seed + idx + next(randomizer)) % len(PRIORITY_ORDER)]
        case_id = f"{screen.name[:3].upper()}-{idx:02d}"
        cases.append(
            TestCase(
                case_id=case_id,
                title=title,
                priority=priority,
                category=category,
                steps=steps,
                expected=expected,
            )
        )

    # Add a goal-focused case for each declared goal
    for goal_index, goal in enumerate(screen.goals, start=len(cases) + 1):
        priority = PRIORITY_ORDER[goal_index % len(PRIORITY_ORDER)]
        cases.append(
            TestCase(
                case_id=f"{screen.name[:3].upper()}-{goal_index:02d}",
                title=f"Goal: {goal}",
                priority=priority,
                category="Goal coverage",
                steps=["Start from the screen", f"Work through: {goal}"],
                expected="User completes the goal without blockers",
            )
        )

    return cases


def build_recommendations(total_cases: int) -> Dict[str, List[str]]:
    return {
        "critical": ["Verify authentication flows before release", "Add uptime monitoring hooks"],
        "quality": [
            "Automate smoke tests for the critical path",
            "Lint accessibility (aria labels, focus traps) in CI",
        ],
        "coverage": [
            f"Plan exploratory testing for the top {max(3, total_cases // 3)} scenarios",
            "Document rollback and recovery procedures",
        ],
    }


def build_summary(project_name: str, screens: List[ScreenInput], total_cases: int) -> Dict[str, str]:
    return {
        "project": project_name,
        "screens": str(len(screens)),
        "test_cases": str(total_cases),
        "risk_level": "Medium" if total_cases < 25 else "High",
        "eta": f"{max(1, total_cases // 5)} QA hours",
    }


def generate_result(session_id: str, payload: Dict) -> GenerationResult:
    screens_output = []
    total_cases = 0

    for screen in payload["screens"]:
        cases = build_test_cases(screen, session_id)
        total_cases += len(cases)
        screens_output.append(
            {
                "name": screen.name,
                "platform": screen.platform,
                "goals": screen.goals,
                "test_cases": [case.__dict__ for case in cases],
            }
        )

    summary = build_summary(payload["project_name"], payload["screens"], total_cases)
    recommendations = build_recommendations(total_cases)

    return GenerationResult(
        session_id=session_id,
        project_name=payload["project_name"],
        created_at=datetime.utcnow().isoformat() + "Z",
        summary=summary,
        screens=screens_output,
        recommendations=recommendations,
    )


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


def run_generation(session_id: str, payload: Dict) -> None:
    with jobs_lock:
        job = jobs[session_id]
        job.status = "running"
        job.progress = 5
        log_job(job, "Queued job")

    try:
        steps = [
            "Validating inputs",
            "Generating scenarios",
            "Scoring test coverage",
            "Finalizing report",
        ]
        for idx, step in enumerate(steps, start=1):
            time.sleep(0.5)
            with jobs_lock:
                job.progress = int((idx / len(steps)) * 90)
                log_job(job, step)

        result = generate_result(session_id, payload)
        with jobs_lock:
            job.result = result
            job.status = "complete"
            job.progress = 100
            log_job(job, "Generation complete", level="success")
    except Exception as exc:  # pragma: no cover - defensive path
        with jobs_lock:
            job.status = "error"
            job.error = str(exc)
            log_job(job, f"Generation failed: {exc}", level="error")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/api/health", methods=["GET"])
def health() -> tuple:
    return jsonify({"status": "ok"}), 200


@app.route("/api/generate", methods=["POST"])
def generate():
    try:
        payload = validate_payload(request.get_json(force=True))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    session_id = uuid.uuid4().hex
    job = JobState(session_id=session_id)

    with jobs_lock:
        jobs[session_id] = job

    worker = threading.Thread(target=run_generation, args=(session_id, payload), daemon=True)
    worker.start()

    return jsonify({"session_id": session_id, "status": "started"})


@app.route("/api/status/<session_id>", methods=["GET"])
def status(session_id: str):
    with jobs_lock:
        job = jobs.get(session_id)
        if not job:
            return jsonify({"error": "Session not found"}), 404

        return jsonify(
            {
                "status": job.status,
                "progress": job.progress,
                "messages": job.messages[-25:],
                "error": job.error,
            }
        )


@app.route("/api/results/<session_id>", methods=["GET"])
def results(session_id: str):
    with jobs_lock:
        job = jobs.get(session_id)
        if not job:
            return jsonify({"error": "Session not found"}), 404
        if job.status != "complete" or not job.result:
            return jsonify({"error": "Results not ready"}), 409

        return jsonify(job.result.__dict__)


def _build_excel(result: GenerationResult) -> BytesIO:
    wb = openpyxl.Workbook()
    summary_sheet = wb.active
    summary_sheet.title = "Summary"
    summary_sheet.append(["Project", result.project_name])
    summary_sheet.append(["Generated", result.created_at])
    summary_sheet.append(["Total screens", result.summary.get("screens")])
    summary_sheet.append(["Total test cases", result.summary.get("test_cases")])
    summary_sheet.append(["Risk", result.summary.get("risk_level")])

    for row in summary_sheet.iter_rows():
        for cell in row:
            cell.font = Font(bold=row[0] is cell)
            cell.alignment = Alignment(vertical="center")

    for screen in result.screens:
        ws = wb.create_sheet(screen["name"][:28] or "Screen")
        ws.append(["Case ID", "Title", "Category", "Priority", "Steps", "Expected"])
        for case in screen["test_cases"]:
            ws.append(
                [
                    case["case_id"],
                    case["title"],
                    case["category"],
                    case["priority"],
                    "\n".join(case["steps"]),
                    case["expected"],
                ]
            )
        for column_cells in ws.columns:
            ws.column_dimensions[column_cells[0].column_letter].width = 20
        for cell in ws[1]:
            cell.font = Font(bold=True)

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


@app.route("/api/report/<session_id>/<format_type>", methods=["GET"])
def download_report(session_id: str, format_type: str):
    with jobs_lock:
        job = jobs.get(session_id)
        if not job:
            return jsonify({"error": "Session not found"}), 404
        if job.status != "complete" or not job.result:
            return jsonify({"error": "Results not ready"}), 409
        result = job.result

    if format_type == "excel":
        buffer = _build_excel(result)
        return send_file(
            buffer,
            download_name=f"{result.project_name}-test-plan.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
        )
    if format_type == "json":
        buffer = BytesIO(json.dumps(result.__dict__).encode("utf-8"))
        buffer.seek(0)
        return send_file(buffer, download_name=f"{result.project_name}-results.json", mimetype="application/json")

    return jsonify({"error": "Unsupported format"}), 400


@app.route("/", methods=["GET"])
def serve_frontend():
    return send_file("frontend2.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
