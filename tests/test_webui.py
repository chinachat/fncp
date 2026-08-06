#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fncp webui.py 安全功能集成测试（Windows 可运行）。

策略：stub 掉 Unix-only 模块 (pty/fcntl/termios)，用假 TermSession 模拟 PTY，
起真实 ThreadingHTTPServer 做端到端 HTTP 断言。

覆盖（v1.1.1）：
  * 强制初始化：未设置密码时终端请求一律 401，设置密码即自动登录
  * 授权密码登录（PBKDF2 存储、HttpOnly Cookie、失败限速锁定）
  * 信任网段 (CIDR) / 信任网址 (Host) 白名单
  * 并发会话上限 / 配置热重载 / 登出

运行: python tests/test_webui.py
"""
import os
import sys
import json
import time
import types
import threading
import urllib.request
import urllib.error

# ---- stub Unix-only modules (Windows) ----
for _m in ("fcntl", "pty", "termios"):
    if _m not in sys.modules:
        sys.modules[_m] = types.ModuleType(_m)
import fcntl  # noqa: E402
import termios  # noqa: E402
fcntl.ioctl = lambda *a, **k: None
termios.TIOCSWINSZ = 0x5414

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))

TEST_CONFIG = os.path.join(HERE, "test-config.json")
if os.path.exists(TEST_CONFIG):
    os.remove(TEST_CONFIG)
os.environ["FNCP_CONFIG"] = TEST_CONFIG
os.environ["FNCP_PORT"] = "18099"

import webui  # noqa: E402

webui.LOGIN_LOCK_SECS = 2  # 测试用短锁定时长

# 假 PTY 会话：首次 read 返回问候语，之后按 hang 决定挂住(keepalive)或结束(None)
class FakeTermSession:
    hang = False

    def __init__(self, token):
        self.token = token
        self.calls = 0
        self.written = b""

    def read(self, timeout=15.0):
        self.calls += 1
        if self.calls == 1:
            return b"hello world\n"
        if self.hang:
            return b""
        return None

    def write(self, data):
        self.written += data
        return True

    def set_size(self, cols, rows):
        pass

    def kill(self):
        pass


webui.TermSession = FakeTermSession

BASE = "http://127.0.0.1:18099"
PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail[:160]) if detail and not cond else ""))


def req(method, path, body=None, cookie=None, host=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    if host:
        r.add_header("Host", host)
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def main():
    srv = webui.ThreadingHTTPServer(("127.0.0.1", 18099), webui.TermHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    print("== 1. 默认配置（未初始化：强制要求先设置密码） ==")
    s, b, h = req("GET", "/api/info")
    j = json.loads(b)
    check("info 200, auth_required=False, version=1.1.1",
          s == 200 and j.get("auth_required") is False and j.get("version") == "1.1.1", b)
    s, b, h = req("GET", "/api/settings")
    check("settings 未初始化时可访问（首次设置入口）", s == 200, b)
    s, b, h = req("GET", "/api/term?token=anon0")
    check("未初始化时终端被拒绝 -> 401", s == 401, b)
    s, b, h = req("POST", "/api/term/input", {"token": "anon0", "data": ""})
    check("未初始化时 input 被拒绝 -> 401", s == 401, b)
    s, b, h = req("POST", "/api/login", {"password": "x"})
    check("未初始化时 login 返回 400", s == 400, b)

    print("== 2. 初始化：设置密码（自动登录） + 白名单 + 并发上限 ==")
    s, b, h = req("POST", "/api/settings", {
        "auth_password": "test123",
        "allowed_cidrs": ["127.0.0.0/8"],
        "trusted_hosts": ["localhost:18099", "127.0.0.1:18099"],
        "max_sessions": 2,
    })
    check("初始化成功且下发登录 Cookie（自动登录）", s == 200 and "Set-Cookie" in h, str(h.get("Set-Cookie")))
    cookie = h["Set-Cookie"].split(";")[0]
    check("Cookie 为 HttpOnly 登录态", cookie.startswith("fncp_auth="), cookie)
    check("config.json 已落盘", os.path.exists(TEST_CONFIG))
    raw = open(TEST_CONFIG, encoding="utf-8").read()
    check("密码以哈希存储（非明文）", "test123" not in raw and "pbkdf2_sha256" in raw, raw[:200])

    print("== 3. 初始化后的鉴权 ==")
    s, b, h = req("GET", "/api/settings")
    check("未登录访问 settings -> 401", s == 401, b)
    s, b, h = req("GET", "/api/term?token=anon1")
    check("未登录访问 term -> 401", s == 401, b)
    s, b, h = req("POST", "/api/term/input", {"token": "anon1", "data": ""})
    check("未登录访问 input -> 401", s == 401, b)

    print("== 4. Host 白名单 ==")
    s, b, h = req("GET", "/api/info", host="evil.example.com")
    check("未信任 Host -> 403", s == 403, b)
    s, b, h = req("GET", "/api/info", host="localhost:18099")
    check("信任 Host(带端口) -> 200", s == 200, b)
    s, b, h = req("GET", "/api/info", host="127.0.0.1:18099")
    check("信任 Host(IP) -> 200", s == 200, b)

    print("== 5. 登录 ==")
    s, b, h = req("POST", "/api/login", {"password": "wrong"})
    check("错误密码 -> 401", s == 401, b)
    s, b, h = req("POST", "/api/login", {"password": "test123"})
    check("正确密码 -> 200 + Set-Cookie", s == 200 and "Set-Cookie" in h, str(h.get("Set-Cookie")))

    print("== 6. 登录后终端全流程 ==")
    s, b, h = req("GET", "/api/term?token=t1", cookie=cookie)
    check("SSE 输出 base64 问候语", s == 200 and "aGVsbG8gd29ybGQK" in b, b[:200])
    check("会话结束后已清理", len(webui.SESSIONS) == 0, str(webui.SESSIONS))

    # 挂起一个会话（hang=True 时 read 一直返回 keepalive），验证 input/resize 写入
    FakeTermSession.hang = True
    threading.Thread(target=lambda: req("GET", "/api/term?token=hang1", cookie=cookie, timeout=5), daemon=True).start()
    time.sleep(0.5)
    check("挂起会话已建立", "hang1" in webui.SESSIONS, str(webui.SESSIONS))
    s, b, h = req("POST", "/api/term/input", {"token": "hang1", "data": "bHMA"}, cookie=cookie)
    check("input 写入会话", s == 200, b)
    s, b, h = req("POST", "/api/term/resize", {"token": "hang1", "cols": 100, "rows": 30}, cookie=cookie)
    check("resize 成功", s == 200, b)
    sess = webui.SESSIONS.get("hang1")
    check("会话收到输入字节", sess is not None and sess.written == b"ls\x00", repr(getattr(sess, "written", None)))

    print("== 7. 设置校验 ==")
    s, b, h = req("POST", "/api/settings", {"allowed_cidrs": ["999.1.1.1/24"]}, cookie=cookie)
    check("非法 CIDR -> 400", s == 400, b)
    s, b, h = req("POST", "/api/settings", {"max_sessions": 0}, cookie=cookie)
    check("非法并发数 -> 400", s == 400, b)
    s, b, h = req("POST", "/api/settings", {"allowed_cidrs": "10.0.0.0/8\n192.168.0.0/16"}, cookie=cookie)
    check("CIDR 支持换行分隔字符串", s == 200 and json.loads(b)["config"]["allowed_cidrs"] == ["10.0.0.0/8", "192.168.0.0/16"], b)

    print("== 8. IP 白名单（含自救路径验证） ==")
    # 当前白名单已是 [10.0.0.0/8, 192.168.0.0/16]，127.0.0.1 不在其中
    s, b, h = req("GET", "/api/info", cookie=cookie)
    check("127.0.0.1 不在白名单 -> 403（已登录也无法自救）", s == 403, b)
    # 自救路径：管理员手动编辑 config.json，热重载立即生效
    with open(TEST_CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["allowed_cidrs"] = ["127.0.0.0/8", "10.0.0.0/8"]
    with open(TEST_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    time.sleep(0.3)
    s, b, h = req("GET", "/api/info", cookie=cookie)
    check("手动编辑 config.json 热重载生效 -> 200", s == 200, b)

    print("== 9. 并发上限 ==")
    # hang1 已挂起（会话数=1）；hang2 建立后（=2）；hang3 应 429
    threading.Thread(target=lambda: req("GET", "/api/term?token=hang2", cookie=cookie, timeout=5), daemon=True).start()
    time.sleep(0.5)
    check("第 2 个会话建立", "hang2" in webui.SESSIONS, str(webui.SESSIONS))
    s, b, h = req("GET", "/api/term?token=hang3", cookie=cookie)
    check("超过上限 -> 429", s == 429, b)
    threading.Thread(target=lambda: req("GET", "/api/term?token=hang1", cookie=cookie, timeout=5), daemon=True).start()
    time.sleep(0.5)
    check("同一 token 重连不新建会话", len(webui.SESSIONS) == 2, str(webui.SESSIONS))
    FakeTermSession.hang = False

    print("== 10. 登出 / 登录限速 / 清除密码 ==")
    s, b, h = req("POST", "/api/logout", cookie=cookie)
    check("logout -> 200 + 清除 Cookie", s == 200 and "Max-Age=0" in h.get("Set-Cookie", ""), b)
    s, b, h = req("GET", "/api/settings", cookie=cookie)
    check("登出后 token 失效 -> 401", s == 401, b)

    # 登录限速：清空计数后连续 5 次错误，第 6 次应被锁定 429
    webui.LOGIN_FAILS.clear()
    codes = []
    for i in range(5):
        s, b, h = req("POST", "/api/login", {"password": "bad%d" % i})
        codes.append(s)
    s, b, h = req("POST", "/api/login", {"password": "bad6"})
    codes.append(s)
    check("连续错误 5 次后锁定 -> 前5次401, 第6次429", codes == [401] * 5 + [429], str(codes))
    s, b, h = req("POST", "/api/login", {"password": "test123"})
    check("锁定期内正确密码也拒绝 -> 429", s == 429, b)
    time.sleep(webui.LOGIN_LOCK_SECS + 0.5)
    s, b, h = req("POST", "/api/login", {"password": "test123"})
    check("解锁后正确密码 -> 200", s == 200, b)
    cookie2 = h["Set-Cookie"].split(";")[0]

    s, b, h = req("POST", "/api/settings", {"auth_password": ""}, cookie=cookie2)
    check("清除密码成功", s == 200, b)
    s, b, h = req("GET", "/api/term?token=anon2")
    check("清除密码后回到未初始化状态，终端拒绝 -> 401", s == 401, b)

    srv.shutdown()
    if os.path.exists(TEST_CONFIG):
        os.remove(TEST_CONFIG)

    print("\n==== 结果: %d PASS / %d FAIL ====" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  FAIL: %s -- %s" % (name, detail))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
