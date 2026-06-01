"""Terminal routes — /api/terminal/* + /ws/terminal/{session_id}.

Spawns pseudo-terminal (pty) processes inside the Odysseus host (WSL/Linux only).
Each session is identified by a UUID; the client opens a WebSocket to stream
bytes both directions. Sessions persist across WS disconnects (the pty stays
alive on the server), so a page reload re-attaches to the same shell.

Architecture:
    POST /api/terminal/spawn         -> {session_id, shell}
    GET  /api/terminal/list          -> active sessions for current user
    POST /api/terminal/{id}/resize   -> resize pty (cols, rows)
    POST /api/terminal/{id}/kill     -> SIGTERM the pty
    WS   /ws/terminal/{id}           -> bidirectional bytes

Bytes from client are written raw to the pty master fd; bytes from pty are
sent back as text frames (UTF-8 decoded with replacement). Resize messages
can also come over the WS as JSON: {"type":"resize","cols":N,"rows":N}.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import termios
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)

DEFAULT_SHELL = os.environ.get("SHELL", "/bin/bash")
DEFAULT_COLS = 100
DEFAULT_ROWS = 30
READ_CHUNK = 4096


def _github_env_for(owner: str | None) -> dict | None:
    """Return env-var overrides for a new pty if the user has GitHub
    integration set up + enabled. Specifically: GITHUB_TOKEN (the canonical
    var `gh` CLI and most github-aware tools read) sourced from the
    decrypted PAT in github_integrations. Returns None on any failure so
    terminal spawn never breaks because of GitHub setup state.

    The cleartext token only ever lives inside the spawned subprocess's
    env block — never written to disk, never echoed in logs."""
    if not owner:
        return None
    try:
        from core.database import SessionLocal as _SL, GitHubIntegration as _GI
        from src.secret_storage import decrypt as _decrypt
    except Exception:
        return None
    try:
        with _SL() as db:
            row = db.query(_GI).filter_by(owner=owner).first()
        if not row or not row.enabled or not row.pat_encrypted:
            return None
        pat = row.pat_encrypted
        if pat.startswith("enc:"):
            pat = _decrypt(pat)
        if not pat:
            return None
        # GH_TOKEN is the modern `gh` CLI preferred env, GITHUB_TOKEN is the
        # broader fallback. Set both so anything in the GitHub ecosystem
        # picks one up.
        return {"GH_TOKEN": pat, "GITHUB_TOKEN": pat}
    except Exception:
        return None


@dataclass
class TerminalSession:
    """One pty session — owns the master fd, the child pid, and metadata."""
    session_id: str
    owner: str
    shell: str
    pid: int
    master_fd: int
    cols: int = DEFAULT_COLS
    rows: int = DEFAULT_ROWS
    cwd: str = ""
    created_at: float = field(default_factory=time.time)
    # Set of WebSockets currently attached. A session can survive with zero
    # WS attached (the pty stays alive); reconnects re-attach.
    attached: set = field(default_factory=set)
    # Recent bytes for replay on reconnect (so attachment shows context).
    scrollback: bytearray = field(default_factory=bytearray)
    scrollback_max: int = 64 * 1024


class TerminalManager:
    """Per-process registry of live pty sessions."""

    def __init__(self):
        self._sessions: Dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()
        self._readers: Dict[str, asyncio.Task] = {}

    def list(self, owner: Optional[str] = None):
        items = self._sessions.values()
        if owner:
            items = [s for s in items if s.owner == owner]
        return [
            {
                "session_id": s.session_id,
                "shell": s.shell,
                "pid": s.pid,
                "cols": s.cols,
                "rows": s.rows,
                "cwd": s.cwd,
                "created_at": s.created_at,
                "attached_count": len(s.attached),
            }
            for s in items
        ]

    def get(self, session_id: str) -> Optional[TerminalSession]:
        return self._sessions.get(session_id)

    async def spawn(
        self,
        owner: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        env_overrides: Optional[Dict[str, str]] = None,
    ) -> TerminalSession:
        """Fork a pty and exec the shell. Returns the registered session."""
        shell = shell or DEFAULT_SHELL
        cwd = cwd or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")

        pid, master_fd = pty.fork()
        if pid == 0:
            # Child: set cwd, env, then exec the shell.
            try:
                os.chdir(cwd)
            except OSError:
                pass
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["COLORTERM"] = "truecolor"
            env["ODYSSEUS_TERMINAL"] = "1"
            if env_overrides:
                env.update(env_overrides)
            # Per-shell startup args: bash/zsh/fish get -l (login shell);
            # Windows-interop shells don't accept POSIX flags.
            base = os.path.basename(shell).lower()
            if base in ("bash", "zsh", "fish"):
                argv = [shell, "-l"]
            else:
                argv = [shell]
            try:
                os.execvpe(shell, argv, env)
            except FileNotFoundError:
                os.execvpe("/bin/sh", ["/bin/sh"], env)
            os._exit(1)

        # Parent: register the session.
        _set_winsize(master_fd, rows, cols)
        # Make the master fd non-blocking so the reader loop can poll.
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        sid = uuid.uuid4().hex
        sess = TerminalSession(
            session_id=sid,
            owner=owner,
            shell=shell,
            pid=pid,
            master_fd=master_fd,
            cols=cols,
            rows=rows,
            cwd=cwd,
        )
        async with self._lock:
            self._sessions[sid] = sess
        # Start the background reader that pumps pty output to attached WSs.
        loop = asyncio.get_running_loop()
        self._readers[sid] = loop.create_task(self._reader(sess))
        logger.info(f"terminal spawned: id={sid[:8]} pid={pid} shell={shell} owner={owner}")
        return sess

    async def write(self, session_id: str, data: bytes):
        sess = self._sessions.get(session_id)
        if not sess:
            return
        try:
            os.write(sess.master_fd, data)
        except OSError as e:
            logger.warning(f"terminal {session_id[:8]} write failed: {e}")

    async def resize(self, session_id: str, cols: int, rows: int):
        sess = self._sessions.get(session_id)
        if not sess:
            return
        sess.cols = cols
        sess.rows = rows
        _set_winsize(sess.master_fd, rows, cols)

    async def kill(self, session_id: str, sig: int = signal.SIGTERM):
        sess = self._sessions.get(session_id)
        if not sess:
            return
        try:
            os.kill(sess.pid, sig)
        except ProcessLookupError:
            pass
        await self._cleanup(session_id)

    async def attach(self, session_id: str, ws: WebSocket) -> Optional[TerminalSession]:
        sess = self._sessions.get(session_id)
        if not sess:
            return None
        sess.attached.add(ws)
        # Replay scrollback so the user sees context after reconnect.
        if sess.scrollback:
            try:
                await ws.send_text(sess.scrollback.decode("utf-8", errors="replace"))
            except Exception:
                pass
        return sess

    async def detach(self, session_id: str, ws: WebSocket):
        sess = self._sessions.get(session_id)
        if sess:
            sess.attached.discard(ws)

    async def _reader(self, sess: TerminalSession):
        """Pumps pty output → all attached WebSockets. Runs until pty closes."""
        loop = asyncio.get_running_loop()
        fd = sess.master_fd
        sid = sess.session_id
        try:
            while True:
                # Wait until fd is readable (or closed).
                fut = loop.create_future()

                def _on_readable():
                    if not fut.done():
                        fut.set_result(None)

                loop.add_reader(fd, _on_readable)
                try:
                    await fut
                finally:
                    try:
                        loop.remove_reader(fd)
                    except Exception:
                        pass

                try:
                    chunk = os.read(fd, READ_CHUNK)
                except OSError:
                    break
                if not chunk:
                    break

                # Append to scrollback (trim to max).
                sess.scrollback.extend(chunk)
                if len(sess.scrollback) > sess.scrollback_max:
                    overflow = len(sess.scrollback) - sess.scrollback_max
                    del sess.scrollback[:overflow]

                # Broadcast to all attached WSs.
                if sess.attached:
                    text = chunk.decode("utf-8", errors="replace")
                    dead = []
                    for ws in list(sess.attached):
                        try:
                            await ws.send_text(text)
                        except Exception:
                            dead.append(ws)
                    for ws in dead:
                        sess.attached.discard(ws)
        except Exception as e:
            logger.exception(f"terminal reader {sid[:8]} crashed: {e}")
        finally:
            logger.info(f"terminal {sid[:8]} pty closed")
            await self._cleanup(sid)

    async def _cleanup(self, session_id: str):
        async with self._lock:
            sess = self._sessions.pop(session_id, None)
        if not sess:
            return
        try:
            os.close(sess.master_fd)
        except OSError:
            pass
        # Tell any attached clients the session ended, then close.
        for ws in list(sess.attached):
            try:
                await ws.send_text("\r\n\x1b[2m[session ended]\x1b[0m\r\n")
                await ws.close()
            except Exception:
                pass
        sess.attached.clear()
        task = self._readers.pop(session_id, None)
        if task and not task.done():
            task.cancel()


def _set_winsize(fd: int, rows: int, cols: int):
    """ioctl TIOCSWINSZ to tell the pty its window size."""
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except Exception as e:
        logger.warning(f"winsize set failed: {e}")


def _discover_shells():
    """Return shells available on this host. WSL gets bonus interop shells."""
    candidates = [
        ("bash", "/bin/bash", None, "Linux bash (default)"),
        ("zsh", "/usr/bin/zsh", None, "Linux zsh"),
        ("fish", "/usr/bin/fish", None, "Linux fish"),
        # WSL interop: launch a real Windows shell from inside WSL.
        ("powershell", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
         "/mnt/c/Users", "Windows PowerShell 5 (via WSL interop)"),
        ("pwsh", "/mnt/c/Program Files/PowerShell/7/pwsh.exe",
         "/mnt/c/Users", "PowerShell 7 (via WSL interop)"),
        ("cmd", "/mnt/c/Windows/System32/cmd.exe",
         "/mnt/c/Users", "Windows cmd.exe (via WSL interop)"),
    ]
    out = []
    for key, path, default_cwd, label in candidates:
        if os.path.exists(path):
            out.append({
                "key": key,
                "command": path,
                "default_cwd": default_cwd,
                "label": label,
            })
    return out


def setup_terminal_routes(manager: TerminalManager) -> APIRouter:
    router = APIRouter(prefix="/api/terminal", tags=["terminal"])

    @router.get("/shells")
    async def shells(request: Request):
        user = get_current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="auth required")
        return {"shells": _discover_shells()}

    @router.post("/spawn")
    async def spawn(request: Request, body: dict = None):
        user = get_current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="auth required")
        body = body or {}
        shell = body.get("shell")
        cwd = body.get("cwd")
        cols = int(body.get("cols") or DEFAULT_COLS)
        rows = int(body.get("rows") or DEFAULT_ROWS)
        # If the user has GitHub integration configured + enabled, surface the
        # PAT to the shell via GITHUB_TOKEN. That's the env var `gh` CLI reads
        # automatically (and the standard for most github-aware tooling), so
        # `gh pr list` etc. just work inside the terminal without any
        # explicit auth step. Decrypts the encrypted PAT on demand; the
        # cleartext only exists in the pty subprocess's env, never written
        # to disk or echoed in our logs.
        env_overrides = _github_env_for(user)
        sess = await manager.spawn(
            owner=user, shell=shell, cwd=cwd, cols=cols, rows=rows,
            env_overrides=env_overrides,
        )
        return {
            "session_id": sess.session_id,
            "shell": sess.shell,
            "pid": sess.pid,
            "cwd": sess.cwd,
            "cols": sess.cols,
            "rows": sess.rows,
        }

    @router.get("/list")
    async def list_sessions(request: Request):
        user = get_current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="auth required")
        return {"sessions": manager.list(owner=user)}

    @router.post("/{session_id}/resize")
    async def resize(session_id: str, request: Request, body: dict):
        user = get_current_user(request)
        sess = manager.get(session_id)
        if not sess or sess.owner != user:
            raise HTTPException(status_code=404, detail="not found")
        await manager.resize(session_id, int(body["cols"]), int(body["rows"]))
        return {"ok": True}

    @router.post("/{session_id}/kill")
    async def kill(session_id: str, request: Request):
        user = get_current_user(request)
        sess = manager.get(session_id)
        if not sess or sess.owner != user:
            raise HTTPException(status_code=404, detail="not found")
        await manager.kill(session_id)
        return {"ok": True}

    return router


def setup_terminal_ws(app, manager: TerminalManager, auth_manager=None,
                      session_cookie_name: str = "odysseus_session",
                      localhost_bypass: bool = True):
    """Register the WebSocket endpoint directly on the FastAPI app.

    auth_manager: the same AuthManager used by the HTTP auth middleware.
        BaseHTTPMiddleware does NOT see WebSocket upgrades, so we authenticate
        the cookie ourselves here. If auth_manager is None, accept anyone
        (dev mode).
    """

    @app.websocket("/ws/terminal/{session_id}")
    async def terminal_ws(ws: WebSocket, session_id: str):
        await ws.accept()
        # --- Auth ---
        user = None
        client_host = ws.client.host if ws.client else None
        if auth_manager is None or not getattr(auth_manager, "is_configured", False):
            # Unconfigured first-run mode: allow only loopback.
            if client_host in ("127.0.0.1", "::1", "localhost"):
                user = "admin"
        else:
            if localhost_bypass and client_host in ("127.0.0.1", "::1"):
                # Match HTTP middleware: loopback gets a free pass when
                # LOCALHOST_BYPASS is on. Use the cookie if present, else
                # fall back to a synthesized admin owner.
                token = ws.cookies.get(session_cookie_name)
                if token and auth_manager.validate_token(token):
                    user = auth_manager.get_username_for_token(token)
                else:
                    user = "admin"
            else:
                token = ws.cookies.get(session_cookie_name)
                if token and auth_manager.validate_token(token):
                    user = auth_manager.get_username_for_token(token)

        if not user:
            await ws.send_text("\x1b[31m[auth required — please log in]\x1b[0m\r\n")
            await ws.close()
            return
        sess = manager.get(session_id)
        if not sess:
            await ws.send_text(f"\x1b[31m[session {session_id[:8]} not found]\x1b[0m\r\n")
            await ws.close()
            return
        if sess.owner != user:
            await ws.send_text("\x1b[31m[not your session]\x1b[0m\r\n")
            await ws.close()
            return

        await manager.attach(session_id, ws)
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                # Two message shapes: raw text (= keyboard input) or JSON control.
                if "text" in msg and msg["text"] is not None:
                    payload = msg["text"]
                    # Try JSON control first (compact heuristic: starts with '{').
                    if payload.startswith("{"):
                        try:
                            obj = json.loads(payload)
                            if obj.get("type") == "resize":
                                await manager.resize(
                                    session_id,
                                    int(obj["cols"]),
                                    int(obj["rows"]),
                                )
                                continue
                            if obj.get("type") == "input" and "data" in obj:
                                await manager.write(session_id, obj["data"].encode("utf-8"))
                                continue
                        except (json.JSONDecodeError, KeyError, ValueError):
                            pass
                    # Default: treat as raw input bytes.
                    await manager.write(session_id, payload.encode("utf-8"))
                elif "bytes" in msg and msg["bytes"] is not None:
                    await manager.write(session_id, msg["bytes"])
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.exception(f"terminal ws {session_id[:8]} crashed: {e}")
        finally:
            await manager.detach(session_id, ws)
