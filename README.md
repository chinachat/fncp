# fncp — 飞牛NAS 命令行文件复制粘贴工具

[![fnOS](https://img.shields.io/badge/fnOS-third--party-orange)](https://www.fnnas.com)
[![Platform](https://img.shields.io/badge/platform-x86%20/%20x86__64-2ea44f)]()
[![Release](https://img.shields.io/badge/release-v1.1.1-blue)](https://github.com/chinachat/fncp/releases)

针对 **飞牛NAS (fnOS)** 的 CLI 工具 + Web 终端。以「剪切板」方式复制/粘贴文件或目录：先 `fcp copy` 记住路径，再 `fcp paste` 执行拷贝，支持跨盘、系统级目录，并完整保留属主、权限与时间戳。

内置 **Web 终端**（xterm.js），安装后在浏览器中直接操作，支持命令颜色、TAB 补全、历史记录，以 **root 权限** 运行。

## 功能特性

- **剪切板式复制粘贴**
  - `fcp copy <路径>...` 复制到剪切板
  - `fcp cut <路径>...` 剪切（移动）到剪切板
  - `fcp paste [目标]` 粘贴到目标目录（默认当前目录）
  - `fcp show / rm <序号> / clear` 查看、移除、清空剪切板
- **sudo / root 权限**：可复制任意系统级文件（`/etc`、docker 卷、其他用户目录等）
- **保留属性**：以 root 执行 `rsync -a`/`cp -a`，完整保留属主、属组、权限、时间戳；有进度条
- **跨文件系统**：剪切模式跨盘自动退化为「复制 + 删除源」
- **Web 终端**：浏览器内 bash，命令高亮、TAB 补全、历史、窗口自适应、选中复制、一键清屏
- **安全防护（v1.1+）**：首次使用强制设置访问密码；支持信任网段/信任网址白名单、并发会话上限、登录失败限速；桌面图标仅管理员可见
- **桌面图标**：安装后飞牛桌面生成图标，点击直达 Web 终端

## 安装

1. 在 [Releases](https://github.com/chinachat/fncp/releases) 下载最新 `fncp_1.1.1_x86.fpk`（或执行 `./build.sh` 自行构建）
2. 打开飞牛 Web 桌面 → **应用中心** → **手动安装**
3. 上传 `.fpk` 文件，按向导完成安装
4. 安装完成后，桌面出现 **fncp** 图标（仅管理员可见），点击打开 Web 终端

> 支持飞牛 x86 / x86_64 机型，fnOS ≥ 0.9.27。

> ⚠️ **升级注意**：从 v1.1.0 及更早版本升级到 v1.1.1 后，若之前未设置过访问密码，首次打开将进入初始化页，**必须设置密码**后才能使用终端；之前已设置密码的则直接要求登录。

## 使用方法

### 命令行（SSH 或 Web 终端）

```bash
fcp help                  # 查看帮助
fcp copy /vol1/影视 /etc/nginx/conf.d     # 记住要复制的路径（可多个）
fcp paste /vol1/backup                    # 粘贴到目标目录
fcp cut  /vol1/旧文件                      # 剪切（移动）
fcp paste ~/回收站
fcp show                  # 查看剪切板内容
fcp rm 2                  # 移除剪切板第 2 项
fcp clear                 # 清空剪切板
```

常用技巧：

```bash
sudo fcp copy /var/lib/docker/volumes     # 复制系统/docker 数据
fcp paste -f /目标                        # 目标已存在时直接覆盖
```

### Web 终端

- 点击飞牛桌面 **fncp** 图标（仅管理员），在浏览器中打开
- **首次使用**：必须先设置访问密码（强制初始化），设置成功后自动进入终端
- 终端以 root 权限运行，可直接操作任意文件
- 支持命令高亮、TAB 补全、方向键历史、选中复制（Ctrl+Shift+C / 右键）、一键清屏
- 输入 `fcp help` 查看用法

## 安全设置

**首次使用强制初始化**：安装后首次打开 Web 终端，必须先设置访问密码才能进入（未设置密码时，后端拒绝一切终端请求，无法绕过）。设置完成后自动登录，后续点击右上角 **⚙ 设置** 可继续配置：

| 防护项 | 说明 | 示例 |
|--------|------|------|
| 访问密码 | 必须输入密码才能打开终端；密码以 PBKDF2-SHA256 哈希存储，登录态为 24 小时 HttpOnly Cookie；同一 IP 连续输错 5 次锁定 60 秒（防暴力破解） | 任意强密码 |
| 信任网段 | 仅允许指定 CIDR 网段的 IP 访问，其余来源直接 403 | `192.168.0.0/24`、`10.0.0.0/8` |
| 信任网址 | 仅允许通过指定 Host 访问（防 IP 直连/域名白名单） | `nas.local`、`192.168.1.5:18018` |
| 并发会话上限 | 限制同时存在的终端会话数，防止资源耗尽 | `10` |

其他说明：

- 配置保存在服务器 `config.json`，**修改即时生效**（无需重启）
- 桌面图标仅对 **管理员** 显示（`allUsers: false`），普通 NAS 用户无法打开应用
- 信任网段/网址留空 = 不限制
- 填错网段导致把自己挡在外面时：通过允许的网段访问后修改，或直接编辑 `config.json`（改动即时生效），或删除该文件恢复出厂（需重新初始化）
- 应用以 **root 权限** 运行，Web 终端等同拥有系统全部权限，**请勿将端口暴露到公网**；仅建议在内网可信环境使用

## 目录结构

```text
fncp/
├── fcp                     # CLI 主程序（bash，仓库根目录副本，与 app/fcp 保持一致）
├── app/
│   ├── fcp                 # 打包用 CLI 主程序
│   ├── webui.py            # Web 终端后端（纯 Python3 标准库，无依赖）
│   └── ui/                 # Web 前端（index.html + xterm.js + 安全设置/登录/初始化页）
├── fnos/                   # fnOS .fpk 打包源
│   ├── manifest            # 应用清单（版本、端口、开发者信息等）
│   ├── Fncp.sc             # 端口转发配置
│   ├── cmd/                # fnOS 生命周期脚本（安装/卸载/启停）
│   ├── config/             # 运行权限配置（run-as: root）
│   ├── wizard/             # 安装向导
│   └── ui/                 # 桌面图标配置（allUsers: false 仅管理员）
├── tests/
│   └── test_webui.py       # 安全功能集成测试（Windows/Linux 均可运行）
├── build.sh                # 构建脚本（生成 dist/*.fpk）
└── dist/                   # 构建产物（fncp_<版本>_x86.fpk）
```

## 构建与测试

在任意有 `bash`、`tar`、`md5sum` 的环境（含飞牛 NAS 本身）执行：

```bash
./build.sh                  # 构建 dist/fncp_<版本>_x86.fpk
```

运行集成测试（纯标准库，Windows/Linux 均可；自动验证安全防护全链路）：

```bash
python tests/test_webui.py
```

## 更新日志

### v1.1.1（2026-08）
- **强制安全初始化**：未设置访问密码时后端拒绝一切终端请求，首次打开必须设置密码
- 首次设置密码成功即自动登录，一步进入终端
- **登录失败限速**：同一 IP 连续输错 5 次锁定 60 秒
- 桌面图标仅管理员可见（`allUsers: false`），普通 NAS 用户无法调用
- 清除密码即回到未初始化状态，终端重新被拒绝

### v1.1.0（2026-08）
- **Web 终端安全防护**（页面 ⚙ 设置，即时生效）：授权密码登录（PBKDF2-SHA256 存储）、信任网段 CIDR 白名单、信任网址 Host 白名单、并发会话上限
- 配置持久化至 `config.json`，支持热重载（手动编辑立即生效）
- 新增集成测试 `tests/test_webui.py`

### v1.0.1（2026-08）
- 首个正式版本：fcp 命令行剪切板工具 + Web 终端（xterm.js）

## 开发者信息

- 维护者 / 发布者：**chinachat**
- 项目主页：https://github.com/chinachat/fncp
