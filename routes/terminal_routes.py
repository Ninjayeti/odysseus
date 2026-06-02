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
import base64
import fcntl
import json
import logging
import os
import pty
import shutil
import signal
import struct
import subprocess
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


def _build_gh_env_command(shell_path: str, on: bool, pat: str | None) -> str:
    """Build the shell-syntax-appropriate command to set/unset the GitHub
    auth env vars in a running pty.

    Per-pty toggle implementation: rather than restarting the shell with
    different env (drastic — loses scrollback, kills any agent the user
    has running in the shell), we write a one-line shell command into the
    pty's stdin. The shell processes it as if the user typed it; the
    export/unset takes effect immediately for the shell and any FUTURE
    subprocess it spawns. Currently-running subprocesses (e.g. a claude
    CLI session) keep their inherited env — they have to be restarted to
    pick up the change.

    Shell detection: we distinguish PowerShell, cmd, and POSIX shells.
    Other shells (e.g. nushell, xonsh) fall through to POSIX syntax —
    most accept the export form, and if they don't the visible error in
    the terminal is honest about what was attempted."""
    shell_base = os.path.basename(shell_path).lower()
    is_pwsh = "powershell" in shell_base or "pwsh" in shell_base
    is_cmd = shell_base in ("cmd.exe", "cmd")

    if is_pwsh:
        if on and pat:
            # Single-quote escaping for PowerShell: '' inside a '...' is a literal '
            esc = pat.replace("'", "''")
            return f"$env:GH_TOKEN = '{esc}'; $env:GITHUB_TOKEN = '{esc}'\r\n"
        return ("Remove-Item Env:GH_TOKEN -ErrorAction SilentlyContinue; "
                "Remove-Item Env:GITHUB_TOKEN -ErrorAction SilentlyContinue\r\n")

    if is_cmd:
        # cmd has no quoted assignment. `set VAR=` empties the variable.
        if on and pat:
            return f"set GH_TOKEN={pat}\r\nset GITHUB_TOKEN={pat}\r\n"
        return "set GH_TOKEN=\r\nset GITHUB_TOKEN=\r\n"

    # POSIX shells (bash, zsh, fish, sh, dash, and the fall-through case)
    if on and pat:
        # bash single-quote escape: close, escape, reopen — '\''
        esc = pat.replace("'", "'\\''")
        return f"export GH_TOKEN='{esc}' GITHUB_TOKEN='{esc}'\n"
    return "unset GH_TOKEN GITHUB_TOKEN\n"


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


def _github_briefing_for(owner: str | None) -> str | None:
    """Return the user's saved GitHub briefing (or the default) when the
    integration is set up + enabled, else None. Mirrors _github_env_for's
    defensive DB access — never raises into the spawn path."""
    if not owner:
        return None
    try:
        from core.database import SessionLocal as _SL, GitHubIntegration as _GI
        from routes.github_routes import DEFAULT_BRIEFING as _DEFAULT
    except Exception:
        return None
    try:
        with _SL() as db:
            row = db.query(_GI).filter_by(owner=owner).first()
        if not row or not row.enabled:
            return None
        return (row.briefing or _DEFAULT).strip() or None
    except Exception:
        return None


# Preamble prepended to the synced CLAUDE.md. The saved briefing was authored
# for Odysseus's chat agent (which has named gh_* helper tools); a terminal
# Claude Code session instead has the `gh` CLI + `git` already authenticated via
# the injected token. This header bridges that gap without editing the briefing.
_TERMINAL_BRIEFING_HEADER = """\
# GitHub working standards (synced from Odysseus — do not edit; changes here are overwritten)

> You are a Claude Code session launched from Odysseus's terminal. Your GitHub
> Personal Access Token is already in the environment (`GH_TOKEN` / `GITHUB_TOKEN`),
> so the `gh` CLI and `git` are authenticated and ready — no auth step needed.
> The standards below were written for Odysseus's chat agent and may reference
> helper tools by name; in this terminal, achieve the same actions with `gh` and
> `git`. Every behavioral rule (honesty, write-action caution, `--force-with-lease`
> only, the commit trailer, PRs ready-but-not-auto-submitted) applies unchanged.
>
> THIS FILE IS A READ-ONLY COPY — it's regenerated from the saved briefing on
> every terminal spawn, so editing it here does nothing. If the user gives you a
> durable GitHub preference, persist it by updating the saved briefing in
> Odysseus (Settings → GitHub → Briefing, or POST /api/github/integration/briefing
> on the local Odysseus server). See "MAINTAINING THIS BRIEFING" below.

---

"""


def _ensure_interop_workdir(owner: str | None) -> str:
    """Resolve the spawn cwd for a Windows-interop shell.

    Why a dedicated dir: launching `claude` in the Windows user home
    (C:\\Users\\<name>) drops you into that "project", which on a busy machine
    is cluttered with background tasks — Claude Code then opens its task list
    instead of a fresh chat. A purpose-built empty dir launches straight into a
    clean session every time.

    Side effect: when the user has GitHub enabled, sync their briefing into a
    CLAUDE.md here so the terminal claude inherits the standards natively (Claude
    Code auto-reads CLAUDE.md from cwd). When disabled, the file is removed so
    stale standards never linger. Falls back to _WIN_INTEROP_CWD on any failure.
    """
    if not _WIN_USER_HOME:
        return _WIN_INTEROP_CWD
    workdir = os.path.join(_WIN_USER_HOME, ".odysseus-shell")
    try:
        os.makedirs(workdir, exist_ok=True)
    except OSError:
        return _WIN_INTEROP_CWD
    # Sync (or clear) the briefing CLAUDE.md. Only rewrite when content actually
    # changed, so we don't churn the file's mtime on every single spawn.
    claude_md = os.path.join(workdir, "CLAUDE.md")
    brief = _github_briefing_for(owner)
    try:
        if brief:
            desired = _TERMINAL_BRIEFING_HEADER + brief + "\n"
            current = None
            if os.path.exists(claude_md):
                with open(claude_md, "r", encoding="utf-8") as fh:
                    current = fh.read()
            if current != desired:
                with open(claude_md, "w", encoding="utf-8") as fh:
                    fh.write(desired)
        elif os.path.exists(claude_md):
            os.remove(claude_md)
    except OSError:
        pass
    return workdir


@dataclass
class TerminalSession:
    """One pty session — owns the master fd, the child pid, and metadata."""
    session_id: str
    owner: str
    shell: str
    pid: int
    master_fd: int                 # read fd: pty master, or broker stdout pipe
    cols: int = DEFAULT_COLS
    rows: int = DEFAULT_ROWS
    cwd: str = ""
    # Broker-backed (native Windows ConPTY) sessions: `proc` is the Windows
    # broker subprocess, `write_fd` is its stdin pipe (framed). For Linux pty
    # sessions proc is None and write_fd == master_fd (raw bidirectional).
    proc: Optional[subprocess.Popen] = None
    write_fd: int = -1
    created_at: float = field(default_factory=time.time)
    # Set of WebSockets currently attached. A session can survive with zero
    # WS attached (the pty stays alive); reconnects re-attach.
    attached: set = field(default_factory=set)
    # Recent bytes for replay on reconnect (so attachment shows context).
    scrollback: bytearray = field(default_factory=bytearray)
    scrollback_max: int = 64 * 1024
    # BEL-strip state machine cursor — preserved across read chunks so an
    # OSC sequence split between two reads still parses correctly. Values:
    # 0 = normal, 1 = just saw ESC, 2 = inside OSC, 3 = saw ESC inside OSC.
    bel_state: int = 0


# Bytes the BEL stripper recognizes.
_BEL = 0x07   # \a — stray alert (drop when not inside OSC)
_ESC = 0x1B   # \x1b — escape
_OSC_OPEN = 0x5D  # ']' — second byte of an OSC opener (ESC ])
_ST_TAIL = 0x5C   # '\\' — second byte of ST terminator (ESC \)


def _strip_stray_bels(chunk: bytes, state: int) -> tuple[bytes, int]:
    """Drop standalone \x07 bytes from the pty stream while preserving BELs
    that are legitimate OSC terminators (e.g. \x1b]0;title\x07 — the
    standard way shells set the window title).

    Why server-side: xterm.js 5.x removed the `bellStyle` option entirely,
    so any consumer-side mute is unreliable; killing the byte before it
    leaves the server guarantees no beep regardless of browser/OS behavior.

    State machine (preserved across chunks via TerminalSession.bel_state):
        0 NORMAL          → ESC takes us to 1, stray BEL is dropped
        1 GOT_ESC         → ']' takes us to 2 (OSC opened), else back to 0
        2 IN_OSC          → BEL (terminator) emitted & back to 0;
                            ESC takes us to 3
        3 GOT_ESC_IN_OSC  → '\\' (ST terminator) emitted & back to 0;
                            ']' opens a new OSC (back to 2);
                            anything else: treat as mid-OSC ESC sequence
                            and stay in OSC (back to 2)

    Returns (cleaned_bytes, new_state).
    """
    out = bytearray()
    for b in chunk:
        if state == 0:
            if b == _BEL:
                continue  # stray alert — drop
            out.append(b)
            if b == _ESC:
                state = 1
        elif state == 1:
            out.append(b)
            state = 2 if b == _OSC_OPEN else 0
        elif state == 2:
            out.append(b)
            if b == _BEL:
                state = 0
            elif b == _ESC:
                state = 3
        elif state == 3:
            out.append(b)
            if b == _ST_TAIL:
                state = 0
            elif b == _OSC_OPEN:
                state = 2
            else:
                state = 2
    return bytes(out), state


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
        """Spawn a shell session. Windows shells go through a native ConPTY
        broker (when available); everything else forks a Linux pty."""
        shell = shell or DEFAULT_SHELL

        # Native Windows ConPTY path for interop shells (pwsh/powershell/cmd).
        if _is_windows_interop_shell(shell) and _WIN_PYTHON and _BROKER_WIN_PATH:
            return await self._spawn_broker(owner, shell, cwd, cols, rows, env_overrides)

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
            elif base in ("pwsh.exe", "powershell.exe", "pwsh", "powershell"):
                # Silence PSReadLine's audible bell at spawn. PSReadLine
                # rings the bell via Console.Beep() — a Windows API call,
                # not a stdout \x07 — so it bypasses the pty entirely and
                # our server-side BEL stripper can't catch it. The bell
                # fires on common interactive actions (backspace past line
                # start, tab-complete with no matches, history nav at top).
                # -NoExit keeps the shell interactive after the command.
                # -ErrorAction SilentlyContinue makes it a no-op if
                # PSReadLine isn't loaded (very old Windows PowerShell).
                argv = [shell, "-NoExit", "-Command",
                        "Set-PSReadLineOption -BellStyle None -ErrorAction SilentlyContinue"]
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

    async def _spawn_broker(self, owner, shell, cwd, cols, rows, env_overrides):
        """Spawn a Windows shell inside a native ConPTY via the Windows broker.
        We talk to the broker over its stdio: framed control in, raw bytes out."""
        win_cwd = _wsl_to_win_path(cwd) if cwd else None

        base = os.path.basename(shell).lower()
        if base in ("pwsh.exe", "powershell.exe"):
            # Same PSReadLine bell-mute as the pty path (Console.Beep bypasses
            # the stream, so this is the only way to silence it).
            argv = [base, "-NoExit", "-Command",
                    "Set-PSReadLineOption -BellStyle None -ErrorAction SilentlyContinue"]
        else:
            argv = [base]

        child_env = {"TERM": "xterm-256color", "COLORTERM": "truecolor",
                     "ODYSSEUS_TERMINAL": "1"}
        if env_overrides:
            child_env.update(env_overrides)

        cfg = {"argv": argv, "cwd": win_cwd, "cols": cols, "rows": rows, "env": child_env}
        b64 = base64.b64encode(json.dumps(cfg).encode("utf-8")).decode("ascii")

        proc = subprocess.Popen(
            [_WIN_PYTHON, _BROKER_WIN_PATH, b64],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        rfd = proc.stdout.fileno()   # read: raw ConPTY output
        wfd = proc.stdin.fileno()    # write: framed control
        for fd in (rfd, wfd):
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        sid = uuid.uuid4().hex
        sess = TerminalSession(
            session_id=sid, owner=owner, shell=shell, pid=proc.pid,
            master_fd=rfd, write_fd=wfd, proc=proc,
            cols=cols, rows=rows, cwd=win_cwd or (cwd or ""),
        )
        async with self._lock:
            self._sessions[sid] = sess
        loop = asyncio.get_running_loop()
        self._readers[sid] = loop.create_task(self._reader(sess))
        logger.info(f"terminal spawned (ConPTY broker): id={sid[:8]} pid={proc.pid} "
                    f"shell={base} cwd={win_cwd} owner={owner}")
        return sess

    async def _drain_write(self, sess: TerminalSession, payload: bytes):
        """Write ALL bytes to the session's write fd, parking on the event loop
        when the pty/pipe buffer is full. The fd is non-blocking, so a single
        os.write of a big buffer (e.g. a clipboard paste) only takes what fits
        and returns short — draining is what fixed the 'can't paste large text'
        bug. Works for both the pty master fd and the broker's stdin pipe."""
        fd = sess.write_fd if sess.write_fd >= 0 else sess.master_fd
        mv = memoryview(payload)
        loop = asyncio.get_event_loop()
        while mv:
            try:
                n = os.write(fd, mv)
                mv = mv[n:]
            except BlockingIOError:
                fut = loop.create_future()
                loop.add_writer(fd, lambda: fut.done() or fut.set_result(None))
                try:
                    await fut
                finally:
                    loop.remove_writer(fd)
            except OSError as e:
                logger.warning(f"terminal {sess.session_id[:8]} write failed: {e}")
                return

    async def write(self, session_id: str, data: bytes):
        sess = self._sessions.get(session_id)
        if not sess:
            return
        # Broker sessions take a framed 'D' message; pty sessions take raw bytes.
        payload = _frame(b"D", data) if sess.proc is not None else data
        await self._drain_write(sess, payload)

    async def resize(self, session_id: str, cols: int, rows: int):
        sess = self._sessions.get(session_id)
        if not sess:
            return
        sess.cols = cols
        sess.rows = rows
        if sess.proc is not None:
            await self._drain_write(sess, _frame(b"R", _BROKER_DIMS.pack(rows, cols)))
        else:
            _set_winsize(sess.master_fd, rows, cols)

    async def kill(self, session_id: str, sig: int = signal.SIGTERM):
        sess = self._sessions.get(session_id)
        if not sess:
            return
        try:
            if sess.proc is not None:
                sess.proc.terminate()      # native Windows broker subprocess
            else:
                os.kill(sess.pid, sig)
        except (ProcessLookupError, OSError):
            pass
        await self._cleanup(session_id)

    async def attach(self, session_id: str, ws: WebSocket) -> Optional[TerminalSession]:
        sess = self._sessions.get(session_id)
        if not sess:
            return None
        sess.attached.add(ws)
        # Replay scrollback so the user sees context after reconnect.
        # Re-run the BEL stripper on replay. The live reader strips bytes
        # as they arrive, but scrollback captured BEFORE the stripper was
        # deployed (long-lived ptys from previous Odysseus versions) can
        # still contain raw \x07 bytes — and replaying those to the
        # browser produces the exact beep on reattach that the live path
        # already eliminates. Using a fresh state=0 here is fine: worst
        # case, an unterminated OSC sequence that spans the scrollback
        # boundary loses its trailing BEL, which means it's left in place
        # (since it isn't standalone), so we never introduce a new beep.
        if sess.scrollback:
            try:
                clean, _ = _strip_stray_bels(bytes(sess.scrollback), 0)
                await ws.send_text(clean.decode("utf-8", errors="replace"))
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

                # Strip stray BELs (preserving OSC terminators) before
                # anything else sees the bytes — so neither the scrollback
                # replay nor the live broadcast can beep on the client.
                chunk, sess.bel_state = _strip_stray_bels(chunk, sess.bel_state)
                if not chunk:
                    continue

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
        if sess.proc is not None:
            # Broker session: stop the Windows subprocess and close its pipes
            # (closing the filenos directly would double-close what Popen owns).
            try:
                sess.proc.terminate()
            except Exception:
                pass
            for stream in (sess.proc.stdin, sess.proc.stdout):
                try:
                    stream.close()
                except Exception:
                    pass
        else:
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


def _detect_windows_user_home() -> str | None:
    """Best-effort: find the current Windows user's home dir from inside
    WSL (e.g. /mnt/c/Users/Matth). Used as the spawn cwd for WSL-interop
    shells so they start in a real user dir instead of C:\\Users.

    Why this matters: when claude is launched in a system-ish dir like
    C:\\Users (which has no .claude/ project state), it falls back to
    its most-recently-active project and effectively cd's there. Users
    perceive this as 'why did claude open my old project?'. Spawning in
    the actual user home avoids the fallback entirely.

    Detection strategy (first match wins):
      1. Single non-system dir under /mnt/c/Users → that one
      2. Multiple dirs → match by current WSL username (with .capitalize()
         since Windows usernames are typically Capitalized)
      3. Fall through to None — caller handles the fallback to /mnt/c/Users
    """
    from pathlib import Path
    users_dir = Path("/mnt/c/Users")
    if not users_dir.is_dir():
        return None
    skip = {"Public", "Default", "Default User", "All Users", "desktop.ini", "WDAGUtilityAccount"}
    try:
        candidates = [p for p in users_dir.iterdir() if p.is_dir() and p.name not in skip]
    except (OSError, PermissionError):
        return None
    if not candidates:
        return None
    if len(candidates) == 1:
        return str(candidates[0])
    # Multi-user box (rare on dev machines, common on shared workstations).
    # Match by WSL username — by far the most common convention is matching
    # case-insensitively but Windows usernames tend to be Capitalized.
    wsl_user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if wsl_user:
        for c in candidates:
            if c.name.lower() == wsl_user.lower():
                return str(c)
    # Couldn't disambiguate — fall through so the caller picks a safe default.
    return None


# Cache the detection at module load. The Windows user home doesn't change
# at runtime, and we don't want to walk /mnt/c/Users on every spawn.
_WIN_USER_HOME = _detect_windows_user_home()
_WIN_INTEROP_CWD = _WIN_USER_HOME or "/mnt/c/Users"


# ── Native Windows ConPTY broker (Option B) ──
# Windows shells (pwsh/powershell/cmd) spawn through a NATIVE ConPTY via
# conpty_broker.py running on Windows Python, instead of a Linux pty driving a
# Windows exe over WSL interop. That interop path is what mistranslated cwd
# (the ~\Autopsy bug), leaked WSL env, and needed the _ensure_interop_workdir
# hack. The broker talks to us over the subprocess's stdio: framed control
# messages in, raw ConPTY bytes out. bash/zsh/fish keep the real Linux pty.
_BROKER_HEADER = struct.Struct(">cI")   # frame: type char + uint32 length
_BROKER_DIMS = struct.Struct(">HH")     # resize payload: rows, cols


def _wsl_to_win_path(p: str) -> str | None:
    r"""/mnt/c/Users/Matth/x -> C:\Users\Matth\x. None if not a /mnt/<drive> path."""
    if not p or not p.startswith("/mnt/"):
        return None
    rest = p[5:]
    drive, _, tail = rest.partition("/")
    if len(drive) != 1:
        return None
    return drive.upper() + ":\\" + tail.replace("/", "\\")


def _is_windows_interop_shell(shell: str | None) -> bool:
    """True for a Windows .exe shell reached via WSL interop (pwsh/powershell/cmd)."""
    return bool(shell and shell.startswith("/mnt/") and shell.lower().endswith(".exe"))


def _frame(kind: bytes, payload: bytes = b"") -> bytes:
    return _BROKER_HEADER.pack(kind, len(payload)) + payload


def _detect_win_python() -> str | None:
    """Find a Windows Python that has pywinpty (the broker's only dependency).
    Skips 3.14 — pywinpty has no wheels for it yet (matches this box's known
    bleeding-edge-runtime breakage)."""
    cands = []
    if _WIN_USER_HOME:
        base = os.path.join(_WIN_USER_HOME, "AppData", "Local", "Programs", "Python")
        for ver in ("Python312", "Python311", "Python310", "Python313"):
            cands.append(os.path.join(base, ver, "python.exe"))
    cands.append("python.exe")   # PATH/interop fallback
    for c in cands:
        try:
            r = subprocess.run([c, "-c", "import winpty"], capture_output=True, timeout=20)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


def _stage_broker() -> str | None:
    """Copy conpty_broker.py to a Windows-local dir and return its WINDOWS path.
    Windows Python launches faster and more reliably from a local path than from
    a \\wsl.localhost UNC path. Refreshed every startup so broker edits propagate."""
    if not _WIN_USER_HOME:
        return None
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "conpty_broker.py")
    if not os.path.isfile(src):
        return None
    dst_dir = os.path.join(_WIN_USER_HOME, "AppData", "Local", "odysseus")
    try:
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "conpty_broker.py")
        shutil.copyfile(src, dst)
    except OSError:
        return None
    return _wsl_to_win_path(dst)


# Resolve once at import. If either piece is missing, Windows shells fall back
# to the legacy WSL-interop pty path (jankier, but still functional).
_WIN_PYTHON = _detect_win_python()
_BROKER_WIN_PATH = _stage_broker()
if _WIN_PYTHON and _BROKER_WIN_PATH:
    logger.info(f"ConPTY broker ready (py={_WIN_PYTHON}, script={_BROKER_WIN_PATH})")
else:
    logger.warning("ConPTY broker unavailable (win python / pywinpty / staging "
                   "missing); Windows shells use the WSL-interop pty fallback")


def _discover_shells():
    """Return shells available on this host. WSL gets bonus interop shells."""
    candidates = [
        ("bash", "/bin/bash", None, "Linux bash (default)"),
        ("zsh", "/usr/bin/zsh", None, "Linux zsh"),
        ("fish", "/usr/bin/fish", None, "Linux fish"),
        # WSL interop: launch a real Windows shell from inside WSL.
        # default_cwd is the user's Windows home if we could detect it,
        # else C:\\Users as a safe fallback. Spawning in the user's home
        # keeps claude / other TUI tools from confusing the dir for a
        # system path and falling back to a "recent project".
        ("powershell", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
         _WIN_INTEROP_CWD, "Windows PowerShell 5 (via WSL interop)"),
        ("pwsh", "/mnt/c/Program Files/PowerShell/7/pwsh.exe",
         _WIN_INTEROP_CWD, "PowerShell 7 (via WSL interop)"),
        ("cmd", "/mnt/c/Windows/System32/cmd.exe",
         _WIN_INTEROP_CWD, "Windows cmd.exe (via WSL interop)"),
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
        # Windows-interop shells (a *.exe under /mnt/c) launch into a dedicated
        # clean workdir instead of the Windows user home: keeps terminal `claude`
        # out of the home dir's background-task list and syncs the GitHub
        # briefing into a CLAUDE.md there. Server-authoritative — overrides
        # whatever default_cwd the client sent. Linux shells are untouched.
        if shell and shell.startswith("/mnt/") and shell.lower().endswith(".exe"):
            cwd = _ensure_interop_workdir(user)
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
                            if obj.get("type") == "set_github":
                                # Per-pty GitHub auth toggle. ON injects an
                                # `export GH_TOKEN=...` line (or equivalent
                                # in PowerShell/cmd); OFF injects `unset`.
                                # The shell processes it as if typed — user
                                # sees the line in their terminal, which is
                                # intentional transparency ("you can see
                                # exactly what changed").
                                on = bool(obj.get("on"))
                                sess = manager.get(session_id)
                                if not sess:
                                    continue
                                pat = None
                                if on:
                                    overrides = _github_env_for(user)
                                    if not overrides:
                                        # Master toggle is off or PAT is
                                        # missing. Tell the user inline so
                                        # they understand why nothing
                                        # happened.
                                        await ws.send_text(
                                            "\r\n\x1b[33m[GitHub integration not "
                                            "configured or paused — re-enable in "
                                            "Settings → Integrations to use this "
                                            "toggle]\x1b[0m\r\n"
                                        )
                                        continue
                                    pat = overrides.get("GH_TOKEN")
                                cmd_text = _build_gh_env_command(sess.shell, on, pat)
                                await manager.write(session_id, cmd_text.encode("utf-8"))
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
