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
You have GitHub tools available. When using them, apply these standards:

QUALITY BAR
- Senior-engineer goggles. Would a senior engineer on this repo's team write this code, in this shape, for this reason? If you'd be embarrassed defending it in code review, redo it.
- First-pass fixes are usually band-aids. Before calling something done, attack it: edge case missed? Scope creep? A shape that papers over the bug class instead of removing it? Fix it before submitting.
- The maintainer's time is the constraint. Every line of diff and every sentence of PR body is a cost to them. Optimize for their attention.

HOW THE WORK GETS DONE
- Verify the bug on clean upstream HEAD first. Don't trust that it reproduces or that it isn't already fixed.
- Match the maintainer's idiom and roadmap. Skim their recent merged PRs and commit messages before drafting.
- Lowest possible effort for the maintainer to merge: rebased on current upstream, minimal diff, clean commit history.

ANTI-PATTERNS
- Don't drop a fix without confirming the bug exists on current upstream.
- Don't make the maintainer choose between approaches in a comment thread — ship cross-linked alternative PRs instead.
- Don't include unrelated formatting changes in the diff.
- Don't refer to maintainer code as a "wart" or "hack" even if it is. Stay neutral.

# ───────────────────────────────────────────────────────────────────
# FILL ME IN — the sections below ship blank on purpose.
# Different models produce different AI-tells (em-dashes, "delve",
# "robust", "leverage", certain rhythms). Your preferred PR voice
# is yours. Tell the agent how you want to sound. Without these,
# PRs will read as generic AI prose — technically correct, no soul.
# ───────────────────────────────────────────────────────────────────

YOUR PR-WRITING VOICE
- (Fill in: how should the agent's PR bodies and comments sound? Formal or casual? Capital I in chat-style writing? Contractions? Short sentences or longer? Match the way YOU'D write the PR if you were typing it yourself.)

ANTI-PATTERNS YOU'VE NOTICED
- (Fill in: list phrases, structures, or tells from your current model that you don't want in your PR bodies. Example: "no em-dashes", "stop using 'wart'", "don't open with 'I hope this helps'". Different models have different tells — calibrate to whatever you're using.)
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
            row.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            return _row_to_dict(row)

    return router
