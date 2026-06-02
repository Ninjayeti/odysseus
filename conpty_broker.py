"""
conpty_broker.py — native Windows ConPTY bridge for Odysseus terminals.

Runs on **Windows Python** (3.12, via WSL interop), NOT inside WSL. The Odysseus
server lives in WSL/Linux where there is no ConPTY; Python's `pty` module there
can only spawn a Linux pseudoterminal and reach Windows shells through the WSL
interop layer — which mistranslates cwd, leaks WSL env, and generally janks out.

This broker owns exactly ONE native ConPTY (via pywinpty, the same backend
Jupyter's terminado uses) and bridges it to the WSL server over plain stdio:

    WSL server  --(framed stdin)-->  broker  --(ConPTY write)-->  shell
    shell       --(ConPTY read)-->   broker  --(raw stdout)----->  WSL server

Wire protocol
-------------
stdin (server → broker): length-framed messages so keystrokes (arbitrary bytes)
and control signals never collide:

    [type:1][length:4 big-endian][payload:length]

    type 'D' (data)   payload = raw bytes to write into the ConPTY
    type 'R' (resize) payload = rows:2 BE + cols:2 BE

stdout (broker → server): the raw ConPTY byte stream, unframed. The server
forwards it straight to xterm.js. Broker process exit == session ended.

Config arrives as a single base64'd-JSON command-line arg, NOT env vars — WSL
only forwards env to Windows processes that are named in WSLENV, so a plain env
hand-off silently drops everything. Command-line args cross the boundary intact.

    argv[1] = base64(json.dumps({
        "argv": ["pwsh.exe", "-NoExit", ...],   # the shell command
        "cwd":  "C:\\Users\\Matth\\.odysseus-shell" | null,
        "cols": 80, "rows": 24,
        "env":  {"GH_TOKEN": "...", "TERM": "xterm-256color", ...}  # merged into child env
    }))
"""

import base64
import json
import os
import struct
import sys
import threading

try:
    from winpty import PtyProcess
except Exception as e:  # pragma: no cover - surfaced to the server's logs
    sys.stderr.write(f"[conpty_broker] pywinpty import failed: {e}\n")
    sys.stderr.flush()
    sys.exit(3)

_HEADER = struct.Struct(">cI")   # type char + uint32 length
_DIMS = struct.Struct(">HH")     # rows, cols


def _read_exact(stream, n):
    """Read exactly n bytes from a binary stream, or return None at EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _stdin_loop(pty, stdin):
    """Parse framed control messages from the server and apply them."""
    while True:
        header = _read_exact(stdin, _HEADER.size)
        if header is None:
            break  # server closed the pipe → tear down
        kind, length = _HEADER.unpack(header)
        payload = _read_exact(stdin, length) if length else b""
        if payload is None:
            break
        try:
            if kind == b"D":
                # pywinpty.write takes str; keystrokes are UTF-8 (terminado does the same).
                pty.write(payload.decode("utf-8", "replace"))
            elif kind == b"R":
                if len(payload) >= _DIMS.size:
                    rows, cols = _DIMS.unpack(payload[: _DIMS.size])
                    pty.setwinsize(rows, cols)
        except Exception:
            # A dead ConPTY surfaces as a write/resize error; the output loop
            # will notice isalive()==False and exit cleanly.
            break


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("[conpty_broker] missing base64 config arg\n")
        return 2
    try:
        cfg = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    except Exception as e:
        sys.stderr.write(f"[conpty_broker] bad config arg: {e}\n")
        return 2

    argv = cfg.get("argv") or []
    if not argv:
        sys.stderr.write("[conpty_broker] empty argv in config\n")
        return 2
    cwd = cfg.get("cwd") or None
    cols = int(cfg.get("cols") or 80)
    rows = int(cfg.get("rows") or 24)

    # Build the ConPTY child env: inherit the broker's own env, then apply the
    # server-supplied overrides (GH_TOKEN, TERM, etc.).
    child_env = os.environ.copy()
    for k, v in (cfg.get("env") or {}).items():
        child_env[str(k)] = str(v)

    if cwd and not os.path.isdir(cwd):
        cwd = None  # let ConPTY default rather than fail the spawn

    try:
        pty = PtyProcess.spawn(argv, cwd=cwd, env=child_env, dimensions=(rows, cols))
    except Exception as e:
        sys.stderr.write(f"[conpty_broker] spawn failed: {e}\n")
        return 4

    stdin = sys.stdin.buffer
    out = sys.stdout.buffer

    t = threading.Thread(target=_stdin_loop, args=(pty, stdin), daemon=True)
    t.start()

    # Output loop: pump ConPTY → stdout until the shell exits.
    try:
        while True:
            try:
                data = pty.read(65536)
            except EOFError:
                break
            if data == "" and not pty.isalive():
                break
            if data:
                out.write(data.encode("utf-8", "replace"))
                out.flush()
    finally:
        try:
            pty.close(force=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
