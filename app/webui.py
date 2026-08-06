#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fncp — 飞牛NAS 文件复制粘贴工具 Web 终端服务

纯 Python3 标准库实现 (http.server + pty + SSE)，无需第三方依赖。
功能：
  * Web 终端 (bash)，支持颜色、TAB 补全
  * 终端内 sudo 免密 (由安装脚本写入 /etc/sudoers.d/fncp)
  * SSE 推送终端输出，POST 传输输入，支持窗口尺寸调整
  * 安全防护（配置存于 config.json，可在页面"安全设置"中修改，即时生效）：
      - 授权密码登录（PBKDF2-SHA256 存储，登录后下发 HttpOnly Cookie）
      - 信任网段白名单（CIDR，如 192.168.0.0/24；空 = 不限制）
      - 信任网址白名单（Host，如 nas.local、192.168.1.5:18018；空 = 不限制）
      - 并发终端会话数上限（防止资源耗尽）
"""
import os
import re
import sys
import json
import time
import hmac
import hashlib
import base64
import signal
import select
import struct
import fcntl
import pty
import termios
import secrets
import threading
import ipaddress
import urllib.parse
from http import cookies as http_cookies
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE_DIR, "ui")
HOME_DIR = os.path.join(BASE_DIR, "home")
PORT = int(os.environ.get("FNCP_PORT", "18018"))
HOST = os.environ.get("FNCP_HOST", "0.0.0.0")
SHELL = os.environ.get("FNCP_SHELL", "/bin/bash")
CONFIG_FILE = os.environ.get("FNCP_CONFIG", os.path.join(BASE_DIR, "config.json"))
APP_VERSION = "1.1.1"

AUTH_COOKIE = "fncp_auth"
AUTH_TOKEN_TTL = 86400  # 登录态有效期: 24 小时

LOGIN_MAX_FAILS = 5     # 同一 IP 连续密码错误次数阈值
LOGIN_LOCK_SECS = 60    # 触发阈值后的锁定秒数

SESSIONS = {}          # 终端会话: token -> TermSession
LOGIN_TOKENS = {}      # 登录态: token -> 过期时间戳
LOGIN_FAILS = {}       # 登录失败计数: ip -> [count, lock_until]
SESS_LOCK = threading.RLock()
CONFIG_LOCK = threading.RLock()

DEFAULT_CONFIG = {
    "allowed_cidrs": [],        # 信任网段, 如 ["192.168.0.0/24", "10.0.0.0/8"]; 空 = 不限制
    "trusted_hosts": [],        # 信任网址 (Host), 如 ["nas.local", "192.168.1.5:18018"]; 空 = 不限制
    "max_sessions": 10,         # 并发终端会话上限
    "auth_password_hash": "",   # pbkdf2_sha256$iter$salt$hash; 空 = 未启用密码
}


def log(*a):
    try:
        print("[fncp]", *a, file=sys.stderr)
    except Exception:
        pass


class Config:
    """config.json 读写（首次运行自动生成默认配置）。"""

    def __init__(self, path):
        self.path = path
        self.data = dict(DEFAULT_CONFIG)
        self._mtime = None
        self._load()

    def _maybe_reload(self):
        """config.json 被外部修改时热重载（管理员手动编辑立即生效）。"""
        with CONFIG_LOCK:
            try:
                mtime = os.path.getmtime(self.path)
            except OSError:
                return
            if mtime != self._mtime:
                self._load()
                self._mtime = mtime

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in DEFAULT_CONFIG:
                if k in data:
                    self.data[k] = data[k]
            self._mtime = os.path.getmtime(self.path)
        except (OSError, ValueError):
            self._save()  # 不存在或损坏时落盘默认配置

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # 原子替换
        except OSError:
            pass

    # ---------- 读取 ----------
    @property
    def allowed_cidrs(self):
        self._maybe_reload()
        return list(self.data.get("allowed_cidrs") or [])

    @property
    def trusted_hosts(self):
        self._maybe_reload()
        return list(self.data.get("trusted_hosts") or [])

    @property
    def max_sessions(self):
        self._maybe_reload()
        try:
            return max(1, int(self.data.get("max_sessions", 10)))
        except (TypeError, ValueError):
            return 10

    @property
    def auth_enabled(self):
        self._maybe_reload()
        return bool(self.data.get("auth_password_hash"))

    # ---------- 密码 ----------
    def set_password(self, plain):
        if plain:
            salt = secrets.token_hex(16)
            dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"),
                                     bytes.fromhex(salt), 600000)
            self.data["auth_password_hash"] = "pbkdf2_sha256$600000$%s$%s" % (salt, dk.hex())
        else:
            self.data["auth_password_hash"] = ""
        self._save()

    def check_password(self, plain):
        stored = self.data.get("auth_password_hash") or ""
        if not stored or not plain:
            return False
        try:
            _, iters, salt, expected = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"),
                                     bytes.fromhex(salt), int(iters))
            return hmac.compare_digest(dk.hex(), expected)
        except (ValueError, TypeError):
            return False

    # ---------- 更新（校验后落盘，非法值抛 ValueError） ----------
    def update(self, **kw):
        with CONFIG_LOCK:
            if "allowed_cidrs" in kw:
                raw = kw["allowed_cidrs"]
                if isinstance(raw, str):
                    raw = re.split(r"[\n,]+", raw)
                cidrs = [c.strip() for c in raw if c and str(c).strip()]
                for c in cidrs:
                    ipaddress.ip_network(c, strict=False)  # 非法 CIDR 抛 ValueError
                self.data["allowed_cidrs"] = cidrs
            if "trusted_hosts" in kw:
                raw = kw["trusted_hosts"]
                if isinstance(raw, str):
                    raw = re.split(r"[\n,]+", raw)
                hosts = [h.strip() for h in raw if h and str(h).strip()]
                self.data["trusted_hosts"] = hosts
            if "max_sessions" in kw:
                n = int(kw["max_sessions"])
                if n < 1 or n > 1000:
                    raise ValueError("并发会话数需在 1~1000 之间")
                self.data["max_sessions"] = n
            if "auth_password" in kw:
                self.set_password(kw["auth_password"] or "")
            self._save()
        return self.public()

    def public(self):
        return {
            "auth_enabled": self.auth_enabled,
            "allowed_cidrs": self.allowed_cidrs,
            "trusted_hosts": self.trusted_hosts,
            "max_sessions": self.max_sessions,
        }


config = Config(CONFIG_FILE)


# ---------- 登录态 ----------
def issue_login_token():
    tok = secrets.token_urlsafe(32)
    with SESS_LOCK:
        LOGIN_TOKENS[tok] = time.time() + AUTH_TOKEN_TTL
    return tok


def check_login(cookie_header):
    if not cookie_header:
        return False
    try:
        c = http_cookies.SimpleCookie(cookie_header)
        tok = c.get(AUTH_COOKIE).value
    except Exception:
        return False
    if not tok:
        return False
    now = time.time()
    with SESS_LOCK:
        exp = LOGIN_TOKENS.get(tok)
        if exp is None:
            return False
        if exp < now:
            del LOGIN_TOKENS[tok]
            return False
        return True


def revoke_login(cookie_header):
    if not cookie_header:
        return
    try:
        tok = http_cookies.SimpleCookie(cookie_header).get(AUTH_COOKIE).value
        with SESS_LOCK:
            LOGIN_TOKENS.pop(tok, None)
    except Exception:
        pass


# ---------- 登录失败限速 ----------
def login_rate_allowed(ip):
    """返回 (是否允许尝试, 剩余锁定秒数)。"""
    now = time.time()
    with SESS_LOCK:
        rec = LOGIN_FAILS.get(ip)
        if not rec:
            return True, 0
        count, lock_until = rec
        if lock_until and now < lock_until:
            return False, int(lock_until - now) + 1
        if lock_until and now >= lock_until:
            rec[0], rec[1] = 0, 0
        return True, 0


def login_fail(ip):
    with SESS_LOCK:
        rec = LOGIN_FAILS.get(ip, [0, 0])
        rec[0] += 1
        if rec[0] >= LOGIN_MAX_FAILS:
            rec[1] = time.time() + LOGIN_LOCK_SECS
            rec[0] = 0
        LOGIN_FAILS[ip] = rec


def login_ok(ip):
    with SESS_LOCK:
        LOGIN_FAILS.pop(ip, None)


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

    # ---------- 安全门禁 ----------
    def _client_ip(self):
        addr = self.client_address[0]
        try:
            ip = ipaddress.ip_address(addr)
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                addr = str(ip.ipv4_mapped)  # ::ffff:127.0.0.1 -> 127.0.0.1
        except ValueError:
            pass
        return addr

    def _ip_allowed(self):
        cidrs = config.allowed_cidrs
        if not cidrs:
            return True
        try:
            ip = ipaddress.ip_address(self._client_ip())
        except ValueError:
            return False
        return any(ip in ipaddress.ip_network(c, strict=False) for c in cidrs)

    def _host_allowed(self):
        hosts = config.trusted_hosts
        if not hosts:
            return True
        raw = self.headers.get("Host") or ""
        try:
            u = urllib.parse.urlsplit("//" + raw)
            req_host = (u.hostname or "").lower()
            req_port = u.port
        except ValueError:
            return False
        if not req_host:
            return False
        for entry in hosts:
            e = entry.strip().lower()
            if not e:
                continue
            try:
                eu = urllib.parse.urlsplit("//" + e)
                eh = (eu.hostname or "").lower()
                ep = eu.port
            except ValueError:
                eh, ep = e, None
            if eh and eh == req_host and (ep is None or ep == req_port or req_port is None):
                return True
        return False

    def _guard(self):
        """IP / Host 白名单：不通过则直接 403。"""
        if not self._ip_allowed():
            self._json({"error": "来源 IP 不在信任网段内"}, 403)
            return False
        if not self._host_allowed():
            self._json({"error": "访问网址(Host)不在信任列表内"}, 403)
            return False
        return True

    def _require_settings_auth(self):
        """设置接口：未启用密码时开放（首次初始化入口）；启用后需登录。"""
        if config.auth_enabled and not check_login(self.headers.get("Cookie")):
            self._json({"error": "未登录或登录已过期"}, 401)
            return False
        return True

    def _require_term_auth(self):
        """终端接口：必须已完成初始化（设置密码）且已登录，否则一律 401。"""
        if not config.auth_enabled:
            self._json({"error": "尚未初始化安全设置，请先设置访问密码"}, 401)
            return False
        if not check_login(self.headers.get("Cookie")):
            self._json({"error": "未登录或登录已过期"}, 401)
            return False
        return True

    # ---------- helpers ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_cookie(self, obj, cookie_line, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", cookie_line)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > 1024 * 1024:  # 请求体上限 1MB
                return {}
            raw = self.rfile.read(n) if n > 0 else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        if not self._guard():
            return
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
                "auth_required": config.auth_enabled,
                "max_sessions": config.max_sessions,
            })
            return
        if path == "/api/term":
            if not self._require_term_auth():
                return
            qs = urllib.parse.parse_qs(parsed.query)
            token = (qs.get("token") or [""])[0]
            if not token:
                self._json({"error": "missing token"}, 400)
                return
            self._stream_term(token)
            return
        if path == "/api/settings":
            if not self._require_settings_auth():
                return
            self._json(config.public())
            return
        super().do_GET()

    def do_HEAD(self):
        if not self._guard():
            return
        super().do_HEAD()

    def _stream_term(self, token):
        with SESS_LOCK:
            session = SESSIONS.get(token)
            if session is None:
                if len(SESSIONS) >= config.max_sessions:
                    self._json({"error": "并发会话数已达上限 (%d)" % config.max_sessions}, 429)
                    return
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
            self.close_connection = True  # SSE 结束后关闭连接，交由基类收尾

    # ---------- POST ----------
    def do_POST(self):
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            body = self._read_json()
            pwd = body.get("password") or ""
            if not config.auth_enabled:
                self._json({"error": "未启用访问密码"}, 400)
                return
            ip = self._client_ip()
            allowed, wait = login_rate_allowed(ip)
            if not allowed:
                self._json({"error": "尝试次数过多，请 %d 秒后再试" % wait}, 429)
                return
            if not config.check_password(pwd):
                login_fail(ip)
                self._json({"error": "密码错误"}, 401)
                return
            login_ok(ip)
            tok = issue_login_token()
            self._json_cookie({"ok": True},
                              "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                              % (AUTH_COOKIE, tok, AUTH_TOKEN_TTL))
            return
        if path == "/api/logout":
            revoke_login(self.headers.get("Cookie"))
            self._json_cookie({"ok": True},
                              "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % AUTH_COOKIE)
            return
        if path == "/api/settings":
            if not self._require_settings_auth():
                return
            body = self._read_json()
            try:
                was_enabled = config.auth_enabled
                new_cfg = config.update(**body)
                if not was_enabled and new_cfg["auth_enabled"]:
                    # 首次初始化：设置密码成功即签发登录态，一步进入终端
                    tok = issue_login_token()
                    self._json_cookie({"ok": True, "config": new_cfg},
                                      "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                                      % (AUTH_COOKIE, tok, AUTH_TOKEN_TTL))
                    return
                self._json({"ok": True, "config": new_cfg})
            except (ValueError, TypeError) as e:
                self._json({"error": str(e)}, 400)
            return
        if path == "/api/term/input":
            if not self._require_term_auth():
                return
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
            if not self._require_term_auth():
                return
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
    log("fncp web UI listening on %s:%d (ui=%s, home=%s, config=%s)"
        % (HOST, PORT, UI_DIR, HOME_DIR, CONFIG_FILE))
    log("security: auth=%s, cidrs=%s, hosts=%s, max_sessions=%d"
        % (config.auth_enabled, config.allowed_cidrs, config.trusted_hosts, config.max_sessions))
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
