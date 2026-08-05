#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fncp — 飞牛NAS 文件复制粘贴工具 Web 终端服务

纯 Python3 标准库实现 (http.server + pty + SSE)，无需第三方依赖。
功能：
  * Web 终端 (bash)，支持颜色、TAB 补全
  * 终端内 sudo 免密 (由安装脚本写入 /etc/sudoers.d/fncp)
  * SSE 推送终端输出，POST 传输输入，支持窗口尺寸调整
"""
import os
import sys
import json
import time
import base64
import signal
import select
import struct
import fcntl
import pty
import termios
import threading
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE_DIR, "ui")
HOME_DIR = os.path.join(BASE_DIR, "home")
PORT = int(os.environ.get("FNCP_PORT", "18018"))
HOST = os.environ.get("FNCP_HOST", "0.0.0.0")
SHELL = os.environ.get("FNCP_SHELL", "/bin/bash")
APP_VERSION = "1.0.0"

SESSIONS = {}
SESS_LOCK = threading.RLock()


def log(*a):
    try:
        print("[fncp]", *a, file=sys.stderr)
    except Exception:
        pass


class TermSession:
    """一次 PTY bash 会话。"""

    def __init__(self, token):
        self.token = token
        self.pid = -1
        self.fd = -1
        self.created = time.time()
        self._spawn()

    def _spawn(self):
        pid, fd = pty.fork()
        if pid == 0:
            # ---- 子进程: bash -------
            try:
                os.environ.setdefault("TERM", "xterm-256color")
                os.environ.setdefault("LANG", "C.UTF-8")
                os.environ.setdefault("LC_ALL", "C.UTF-8")
                if os.path.isdir(HOME_DIR):
                    os.chdir(HOME_DIR)
                    os.environ["HOME"] = HOME_DIR
                else:
                    os.chdir("/")
                os.execv(SHELL, [SHELL, "-i"])
            except Exception as e:
                os.write(2, ("fncp: exec failed: %s\n" % e).encode())
                os._exit(1)
        self.pid = pid
        self.fd = fd
        self.set_size(80, 24)

    def set_size(self, cols, rows):
        if self.fd < 0:
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", int(rows), int(cols), 0, 0))
        except (OSError, ValueError):
            pass

    def read(self, timeout=15.0):
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not r:
            return b""
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            return None
        if not data:
            return None
        return data

    def write(self, data):
        if self.fd < 0 or not data:
            return False
        try:
            os.write(self.fd, data)
            return True
        except OSError:
            return False

    def kill(self):
        if self.pid > 0:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except OSError:
                pass
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = -1
        self.pid = -1


class TermHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UI_DIR, **kwargs)

    # ---------- helpers ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._json({"status": "ok", "time": int(time.time())})
            return
        if path == "/api/info":
            self._json({
                "app": "fncp",
                "version": APP_VERSION,
                "shell": SHELL,
                "port": PORT,
            })
            return
        if path == "/api/term":
            qs = urllib.parse.parse_qs(parsed.query)
            token = (qs.get("token") or [""])[0]
            if not token:
                self._json({"error": "missing token"}, 400)
                return
            self._stream_term(token)
            return
        super().do_GET()

    def _stream_term(self, token):
        with SESS_LOCK:
            session = SESSIONS.get(token)
            if session is None:
                session = TermSession(token)
                SESSIONS[token] = session
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                data = session.read(timeout=15.0)
                if data is None:
                    break
                if data:
                    chunk = base64.b64encode(data).decode("ascii")
                    self.wfile.write(("data: %s\n\n" % chunk).encode("ascii"))
                    self.wfile.flush()
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with SESS_LOCK:
                if SESSIONS.get(token) is session:
                    del SESSIONS[token]
            session.kill()
            try:
                self.wfile.close()
            except Exception:
                pass

    # ---------- POST ----------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/term/input":
            body = self._read_json()
            token = body.get("token", "")
            with SESS_LOCK:
                session = SESSIONS.get(token)
            if session is None:
                self._json({"error": "no session"}, 404)
                return
            data = body.get("data", "") or ""
            try:
                payload = base64.b64decode(data)
            except Exception:
                payload = b""
            session.write(payload)
            self._json({"ok": True})
            return
        if path == "/api/term/resize":
            body = self._read_json()
            token = body.get("token", "")
            with SESS_LOCK:
                session = SESSIONS.get(token)
            if session is None:
                self._json({"error": "no session"}, 404)
                return
            try:
                cols = int(body.get("cols", 80))
                rows = int(body.get("rows", 24))
            except (TypeError, ValueError):
                cols, rows = 80, 24
            session.set_size(cols, rows)
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), TermHandler)
    httpd.daemon_threads = True
    log("fncp web UI listening on %s:%d (ui=%s, home=%s)" % (HOST, PORT, UI_DIR, HOME_DIR))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        with SESS_LOCK:
            for s in list(SESSIONS.values()):
                s.kill()


if __name__ == "__main__":
    main()
