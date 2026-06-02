"""Canvas LMS integration — wraps CanvasSync's API for agent tool use."""

import json
import re
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

# CanvasSync config lives on the Windows side (WSL path)
_CANVAS_CONFIG_PATHS = [
    Path("/mnt/c/Users/Matth/CanvasSync/config.json"),
    Path.home() / "CanvasSync" / "config.json",
]

COURSE_SHORT = {
    2661345: "CMST 280",
    2661976: "PHIL 101",
    2662059: "POLS 201",
    2697597: "CJ&101",
    2697561: "ECON&202",
    2698310: "POLS206",
}


def _load_config() -> Optional[Dict]:
    for p in _CANVAS_CONFIG_PATHS:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Canvas config at {p} unreadable: {e}")
    return None


def _short_name(full_name: str) -> str:
    m = re.match(r'([A-Z]{2,8}(?:[&\s])?\d{2,4})', full_name or "")
    return m.group(1) if m else (full_name or "")[:14]


class CanvasClient:
    def __init__(self, base_url: str, token: str):
        import requests
        self.base = base_url.rstrip("/") + "/api/v1"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, path, params=None):
        r = self.session.get(f"{self.base}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_active_courses(self) -> List[Dict]:
        courses = self._get("/courses", params={
            "enrollment_state": "active", "per_page": 50,
        }) or []
        return [
            c for c in courses
            if isinstance(c, dict) and c.get("id")
            and c.get("workflow_state") == "available"
            and re.match(r'[A-Z]{2,8}(?:[&\s])?\d{2,4}', c.get("name", ""))
        ]

    def get_assignments(self, course_id: int, days_ahead: int = 14) -> List[Dict]:
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days_ahead)
        assignments = []
        page = 1
        while True:
            data = self._get(f"/courses/{course_id}/assignments", params={
                "per_page": 50, "page": page,
                "order_by": "due_at", "include[]": "submission",
            })
            if not data:
                break
            for a in data:
                due = a.get("due_at")
                if not due:
                    continue
                due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                sub = a.get("submission", {})
                ws = sub.get("workflow_state")
                if ws == "graded":
                    continue
                if due_dt < now or due_dt > end:
                    continue
                a["_due_dt"] = due_dt.isoformat()
                a["_submitted"] = ws == "submitted"
                assignments.append(a)
            if len(data) < 50:
                break
            page += 1
        return sorted(assignments, key=lambda x: x.get("_due_dt", ""))

    def get_missing(self, course_id: int) -> List[Dict]:
        data = self._get(f"/courses/{course_id}/assignments", params={
            "bucket": "past", "per_page": 100, "include[]": "submission",
            "order_by": "due_at",
        })
        missing = []
        for a in (data or []):
            sub = a.get("submission") or {}
            if sub.get("workflow_state") not in ("submitted", "graded"):
                due = a.get("due_at")
                if due:
                    a["_due_dt"] = datetime.fromisoformat(due.replace("Z", "+00:00")).isoformat()
                    missing.append(a)
        return missing

    def get_grades(self, course_id: int) -> Dict:
        try:
            enrollments = self._get(f"/courses/{course_id}", params={
                "include[]": "total_scores",
            })
            return enrollments
        except Exception:
            return {}

    def get_inbox(self, per_page: int = 10) -> List[Dict]:
        return self._get("/conversations", params={"per_page": per_page}) or []


def _fmt_assignment(a: Dict, course_name: str) -> str:
    name = a.get("name", "Unknown")
    due = a.get("_due_dt", "?")
    pts = a.get("points_possible")
    pts_str = f"{int(pts)} pts" if pts else ""
    submitted = " [SUBMITTED]" if a.get("_submitted") else ""
    types = ", ".join(a.get("submission_types") or [])
    return f"- {course_name}: {name} — due {due} {pts_str} ({types}){submitted}"


async def canvas_tool(action: str, **kwargs) -> Dict[str, Any]:
    """Main entry point for the canvas_sync agent tool."""
    cfg = _load_config()
    if not cfg or not cfg.get("api_token"):
        return {"error": "Canvas not configured. CanvasSync config.json not found or missing api_token."}

    try:
        client = CanvasClient(cfg["canvas_url"], cfg["api_token"])
    except Exception as e:
        return {"error": f"Failed to connect to Canvas: {e}"}

    course_ids = cfg.get("course_ids", [])
    days_ahead = cfg.get("days_ahead", 14)

    if action == "assignments":
        results = []
        for cid in course_ids:
            cname = COURSE_SHORT.get(cid, f"Course {cid}")
            try:
                assignments = client.get_assignments(cid, days_ahead)
                for a in assignments:
                    results.append(_fmt_assignment(a, cname))
            except Exception as e:
                results.append(f"- {cname}: error fetching — {e}")
        if not results:
            return {"result": "No upcoming assignments in the next {days_ahead} days."}
        return {"result": f"Upcoming assignments ({days_ahead} days):\n" + "\n".join(results)}

    elif action == "missing":
        results = []
        for cid in course_ids:
            cname = COURSE_SHORT.get(cid, f"Course {cid}")
            try:
                missing = client.get_missing(cid)
                for a in missing:
                    results.append(_fmt_assignment(a, cname))
            except Exception as e:
                results.append(f"- {cname}: error — {e}")
        if not results:
            return {"result": "No missing assignments. You're caught up!"}
        return {"result": f"Missing/overdue assignments:\n" + "\n".join(results)}

    elif action == "courses":
        try:
            courses = client.get_active_courses()
            lines = [f"- {_short_name(c.get('name', ''))} (id={c['id']})" for c in courses]
            return {"result": "Active courses:\n" + "\n".join(lines)}
        except Exception as e:
            return {"error": f"Failed to list courses: {e}"}

    elif action == "inbox":
        try:
            messages = client.get_inbox(per_page=10)
            lines = []
            for m in messages:
                subj = m.get("subject", "(no subject)")
                last = m.get("last_message", "")[:80]
                wf = m.get("workflow_state", "")
                lines.append(f"- [{wf}] {subj}: {last}...")
            if not lines:
                return {"result": "Inbox is empty."}
            return {"result": "Recent Canvas inbox:\n" + "\n".join(lines)}
        except Exception as e:
            return {"error": f"Failed to fetch inbox: {e}"}

    else:
        return {"error": f"Unknown action '{action}'. Use: assignments, missing, courses, inbox"}
