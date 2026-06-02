"""
github_routes.py

REST endpoints for the per-user GitHub integration: save / clear the
Personal Access Token, fetch + edit the agent briefing, toggle the
write-permission flag. The actual GitHub API calls live in
`mcp_servers/github_server.py`; this file just manages the integration
config row in the DB.

Auth model: every endpoint takes the current Odysseus user via
`require_user` (matches the email_routes pattern). Each user owns one
GitHub integration row keyed by username.

PAT storage: encrypted at rest via `src/secret_storage.py`. Plaintext
PAT is only present in memory during a save (to validate against
GitHub) and inside the github MCP subprocess when it builds an
auth header. We never echo the PAT back in API responses — the GET
endpoint returns metadata only (username, enabled flags, briefing).
"""

import logging
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request, Body
from pydantic import BaseModel, Field

from core.database import SessionLocal, GitHubIntegration
from routes.email_helpers import require_user
from src.secret_storage import encrypt as _encrypt_secret

logger = logging.getLogger(__name__)

GH_API = "https://api.github.com"
GH_TIMEOUT = 15.0
USER_AGENT = "Odysseus-Integration/0.1"

# Default briefing seeded for new users. Trimmed from the longer personal
# brief — readable in one sitting, gets the agent to act like a competent
# contributor without dictating an exhaustive style. User-editable.
DEFAULT_BRIEFING = """\
You have GitHub tools available. When using them, apply these standards.
This briefing is repo-agnostic — it should hold whether you're contributing
to someone else's open-source project, working on the user's own fork, or
shipping changes inside an internal codebase. The audience for your output
is "whoever reads this next" — the maintainer, a teammate, or the user's
future self in six months. Optimize for their attention, not yours.

QUALITY BAR
- Code you'd be willing to defend in review. If you'd cringe explaining a choice to a strong engineer, redo it before submitting.
- First-pass fixes are usually band-aids. Before calling something done: edge case missed? Scope creep? A shape that papers over the bug class instead of removing it? Fix the cause, not the symptom.
- Reader-time is the scarce resource. Every line of diff and every sentence of PR body is a cost. Earn each one.

HONESTY
- If you didn't run something, don't say you did. "I tested this" means you actually executed it and observed the result. "This should work" or "this compiles cleanly in my head" is fine when stated honestly.
- If you're uncertain about an API, file path, syntax, or behavior, check the actual code or docs before using it. A confident guess fails silently and burns reader trust harder than admitting "I don't know, let me check."
- When the user references an issue, PR, or commit by number, fetch it before assuming what it's about from the title.
- When you report that something changed — a notification cleared, an issue closed, a check flipped — establish the real cause before describing it. Fetch the thread or run timeline (close reason, who acted, the linked/merged PR); never infer the reason from earlier conversation. A status delta reported without its verified cause is a guess dressed as an observation.
- Don't let a narrow view fool you into reporting "nothing changed." Notification inboxes default to unread-only, so anything read between checks disappears from that view. Diff against what you've already seen — by id and last-updated time — not against what merely remains unread.

HOW THE WORK GETS DONE
- Verify the bug or starting condition first on the actual branch the work will land on. Don't trust that it reproduces, or that it isn't already fixed somewhere you haven't looked.
- Match the codebase's existing idiom. Skim recent merged PRs and a few representative files in the area before drafting. Style consistency matters more than your personal preference.
- Smallest viable diff. If you're tempted to add tests, refactor adjacent code, or "while I'm here" cleanups — don't. Mention them in the PR body as follow-ups instead of smuggling them in the diff.

WRITE ACTIONS (commits, PR comments, opening/editing PRs, pushes)
- Before any write action, briefly say WHAT you're about to do and WHY in your reply. One or two sentences. This is not asking for approval — it's giving the user a chance to intervene mid-thread if your plan is off. They already opted into write actions in settings; they don't want to re-approve each one, they want to be informed so they can course-correct.
- After a write action, briefly state WHAT you did and link to it (PR URL, commit SHA, comment permalink). Don't make the user hunt for the result.

ANTI-PATTERNS
- Don't drop a fix without confirming the bug exists on the current branch.
- Don't make the reader choose between approaches in a comment thread — ship cross-linked alternative PRs instead.
- Don't include unrelated formatting changes in the diff.
- Don't claim work is "tested" without actually running the tests.
- Don't add scope the user didn't ask for. If you think it's important, surface it as a follow-up suggestion in the PR body.

MAINTAINING THIS BRIEFING (you can edit your own standards)
- This briefing IS your GitHub standard — it's stored in Odysseus and injected into you every session. When the user expresses a DURABLE preference ("I like my PRs written like this", "always squash", "never use em-dashes", "match this commit style"), don't just honor it for one reply — fold it into the right section below (VOICE / COMMIT STYLE / ANTI-PATTERNS) so it persists across sessions and to the other agent (chat ↔ terminal share this one briefing).
- Distinguish durable from one-off: "rewrite this PR body" is one-off; "I always want PR bodies to lead with the why" is durable → update the briefing. When unsure, ask "want me to make that a standing rule?" before editing.
- Where to edit it: it's a single source of truth, editable in Odysseus → Settings → GitHub → Briefing. Persist changes by saving that text (POST /api/github/integration/briefing with the full updated markdown). If you're a terminal session, your local CLAUDE.md copy is READ-ONLY and regenerated from this saved briefing every spawn — editing the file won't stick; update the saved briefing instead.

YOUR PR-WRITING VOICE
- Replies to reviewers/maintainers on a PR are terse — one line. "Fixed in <sha>, thanks for the heads up." is plenty. Don't restate the diff, re-explain the fix, or thank at length; the commit and the code speak for themselves. This brevity rule is specifically for back-and-forth replies — a PR body itself can run longer when the change warrants it.
- Often the right move is no reply at all: when a reviewer asks for changes, just make them and push. Let the new commit answer the comment instead of a comment plus a commit.

COMMIT MESSAGE STYLE
- (Fill in: Conventional Commits? Imperative mood? Subject cap? Body wrap?)

AI-ATTRIBUTION
- Keep the `Co-Authored-By: Claude <noreply@anthropic.com>` trailer on commits you author — it's a deliberate disclosure that an agent co-authored the work, so don't strip it. No separate "Generated with Claude" footer in PR bodies, though; the commit trailer is the disclosure, the body should read as the user's own.

ANTI-PATTERNS YOU'VE NOTICED
- No needy closers or AI-assistant tics on GitHub comments. Don't end with "Ready for another look", "Let me know if you'd like any changes", "Happy to adjust", "Hope this helps", or "Can I help with anything else". State what changed and stop — a maintainer re-reviews on their own schedule and doesn't need to be invited to. A status comment ends on the fact, not on a prompt for attention.
- No em-dashes in PR bodies or comments. Use periods and commas, or restructure the sentence.
"""

# Used by the settings card to detect "user hasn't filled in their style yet"
# and show a soft banner nudging them to. Matches the section markers in
# DEFAULT_BRIEFING; if the user removes the markers it stops nagging.
_UNFILLED_MARKERS = ("(Fill in:",)


# ── Pydantic request bodies ──

class SavePATRequest(BaseModel):
    pat: str = Field(..., min_length=1, description="GitHub Personal Access Token. Validated against /user before save.")


class UpdateBriefingRequest(BaseModel):
    briefing: str = Field(..., description="Markdown briefing text. Empty string resets to the default.")


class UpdateFlagsRequest(BaseModel):
    enabled: bool | None = None
    write_enabled: bool | None = None
    notify_enabled: bool | None = None


# ── Helpers ──

def _briefing_is_unfilled(text: str | None) -> bool:
    """True if the briefing still contains the (Fill in: ...) prompts that
    ship in the default. Used to nag the user in Settings until they
    customize the style sections."""
    if not text:
        return True
    return any(marker in text for marker in _UNFILLED_MARKERS)


def _row_to_dict(row: GitHubIntegration) -> dict:
    """Public view of the integration row. NEVER includes the PAT."""
    briefing = row.briefing or DEFAULT_BRIEFING
    return {
        "configured": True,
        "github_username": row.github_username,
        "enabled": bool(row.enabled),
        "write_enabled": bool(row.write_enabled),
        "notify_enabled": bool(getattr(row, "notify_enabled", False)),
        "briefing": briefing,
        "briefing_unfilled": _briefing_is_unfilled(briefing),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _validate_pat(pat: str) -> dict:
    """Hit GitHub /user with the PAT. Returns the user object on success.
    Raises HTTPException(400) with a useful message on failure so the
    settings UI can surface what went wrong."""
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(timeout=GH_TIMEOUT) as client:
            resp = await client.get(f"{GH_API}/user", headers=headers)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {e}")
    if resp.status_code == 401:
        raise HTTPException(status_code=400, detail="GitHub rejected the token (401). Check the PAT value and scopes.")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"GitHub returned {resp.status_code}: {resp.text[:160]}")
    try:
        return resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="GitHub returned a non-JSON response.")


# ── Router setup ──

def setup_github_routes(mcp_manager=None):
    """Build the router. `mcp_manager` is optional; if passed, we restart the
    github MCP server after a PAT change so its in-process PAT cache flushes.
    Without it, a server restart is needed for new PATs to take effect."""
    router = APIRouter(prefix="/api/github", tags=["github"])

    async def _restart_github_mcp(owner: str):
        if not mcp_manager:
            return
        try:
            # The github MCP server caches the PAT in-process on first use.
            # After a save we want it to re-read from the DB. Easiest path
            # is to disconnect + reconnect; mcp_manager handles spawning.
            if hasattr(mcp_manager, "disconnect_server"):
                await mcp_manager.disconnect_server("github")
            # The connect happens automatically next time a github tool is
            # called (or you can manually re-trigger register_builtin_servers).
            # For now, just disconnect — next request reconnects.
        except Exception as e:
            logger.warning(f"github MCP restart after PAT save failed (non-fatal): {e}")

    @router.get("/integration")
    def get_integration(request: Request):
        """Return the current user's integration metadata, or
        {configured: false} if none. Never returns the PAT."""
        owner = require_user(request)
        with SessionLocal() as db:
            row = db.query(GitHubIntegration).filter_by(owner=owner or "").first()
        if not row:
            return {
                "configured": False,
                "briefing": DEFAULT_BRIEFING,
                "briefing_unfilled": True,
            }
        return _row_to_dict(row)

    @router.post("/integration")
    async def save_integration(request: Request, body: SavePATRequest):
        """Save (or replace) the user's PAT. Validates the token against
        /user first; on success persists encrypted PAT + username. On
        failure returns 400 with GitHub's error message."""
        owner = require_user(request)
        pat = body.pat.strip()
        if not pat:
            raise HTTPException(400, "PAT is empty.")
        user_obj = await _validate_pat(pat)
        gh_username = user_obj.get("login") or "unknown"
        enc = _encrypt_secret(pat)
        with SessionLocal() as db:
            row = db.query(GitHubIntegration).filter_by(owner=owner or "").first()
            if row:
                row.pat_encrypted = enc
                row.github_username = gh_username
                row.enabled = True
                row.updated_at = datetime.utcnow()
            else:
                row = GitHubIntegration(
                    owner=owner or "",
                    pat_encrypted=enc,
                    github_username=gh_username,
                    briefing=DEFAULT_BRIEFING,
                    enabled=True,
                    write_enabled=False,
                )
                db.add(row)
            db.commit()
            db.refresh(row)
            payload = _row_to_dict(row)
        await _restart_github_mcp(owner or "")
        return payload

    @router.delete("/integration")
    async def delete_integration(request: Request):
        """Remove the integration entirely. PAT, username, briefing — gone."""
        owner = require_user(request)
        with SessionLocal() as db:
            row = db.query(GitHubIntegration).filter_by(owner=owner or "").first()
            if row:
                db.delete(row)
                db.commit()
        await _restart_github_mcp(owner or "")
        return {"configured": False}

    @router.post("/integration/briefing")
    def update_briefing(request: Request, body: UpdateBriefingRequest):
        """Update the agent briefing text. Empty string resets to default."""
        owner = require_user(request)
        with SessionLocal() as db:
            row = db.query(GitHubIntegration).filter_by(owner=owner or "").first()
            if not row:
                raise HTTPException(404, "GitHub integration not configured.")
            row.briefing = body.briefing.strip() or DEFAULT_BRIEFING
            row.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            return _row_to_dict(row)

    @router.post("/integration/flags")
    def update_flags(request: Request, body: UpdateFlagsRequest):
        """Update enabled / write_enabled toggles independently."""
        owner = require_user(request)
        with SessionLocal() as db:
            row = db.query(GitHubIntegration).filter_by(owner=owner or "").first()
            if not row:
                raise HTTPException(404, "GitHub integration not configured.")
            if body.enabled is not None:
                row.enabled = bool(body.enabled)
            if body.write_enabled is not None:
                row.write_enabled = bool(body.write_enabled)
            if body.notify_enabled is not None:
                row.notify_enabled = bool(body.notify_enabled)
            row.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            return _row_to_dict(row)

    # ── Notifications poll endpoint ──
    # Cheap proxy for GET /notifications on GitHub. The frontend hits this
    # every ~90s when notify_enabled. Server-side cache (60s) absorbs
    # multi-tab spam so we never exceed GitHub's 60-rpm threshold even with
    # several Odysseus windows open.
    _POLL_CACHE_TTL = 60.0  # seconds

    @router.get("/notifications/count")
    async def notifications_count(request: Request):
        owner = require_user(request)
        with SessionLocal() as db:
            row = db.query(GitHubIntegration).filter_by(owner=owner or "").first()
            if not row or not row.enabled or not row.notify_enabled:
                return {"count": 0, "notify_enabled": False, "cached": False}
            now = datetime.utcnow()
            last = row.last_notif_polled_at
            age = (now - last).total_seconds() if last else None
            if age is not None and age < _POLL_CACHE_TTL:
                return {
                    "count": int(row.last_notif_count or 0),
                    "notify_enabled": True,
                    "cached": True,
                    "age_seconds": int(age),
                }
            # Cache stale (or never polled) — hit GitHub.
            pat = row.pat_encrypted or ""
            if pat.startswith("enc:"):
                from src.secret_storage import decrypt as _decrypt
                pat = _decrypt(pat)
            headers = {
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
            }
            count = 0
            try:
                async with httpx.AsyncClient(timeout=GH_TIMEOUT) as client:
                    # `all=false` (default) returns only unread.
                    resp = await client.get(f"{GH_API}/notifications", headers=headers, params={"per_page": 50})
                if resp.status_code == 200:
                    count = len(resp.json() or [])
                else:
                    logger.warning(f"github notif poll returned {resp.status_code}")
            except Exception as e:
                logger.warning(f"github notif poll failed: {e}")
                # Return last-known count so a transient outage doesn't blank the badge.
                return {"count": int(row.last_notif_count or 0), "notify_enabled": True, "cached": True, "stale": True}
            # Persist
            row.last_notif_count = count
            row.last_notif_polled_at = now
            db.commit()
            return {"count": count, "notify_enabled": True, "cached": False}

    return router
