#!/bin/bash
# fncp — 构建 fnOS (.fpk) 安装包
# 在 bash 环境运行: ./build.sh
# 产物输出到 dist/ 目录。
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
FNOS="$ROOT/fnos"
APP="$ROOT/app"
DIST="$ROOT/dist"
WORK="$ROOT/.build"

log() { echo "[build] $*"; }

require() {
    for p in "$@"; do
        [ -e "$p" ] || { echo "[build] ERROR: missing: $p"; exit 1; }
    done
}

require \
    "$FNOS/manifest" "$FNOS/Fncp.sc" \
    "$FNOS/ICON.PNG" "$FNOS/ICON_256.PNG" \
    "$FNOS/cmd" "$FNOS/config" \
    "$FNOS/ui/config" "$FNOS/ui/images/256.png" "$FNOS/ui/images/64.png" \
    "$APP/fcp" "$APP/webui.py" \
    "$APP/ui/index.html" \
    "$APP/ui/vendor/xterm.js" "$APP/ui/vendor/xterm.css" \
    "$APP/ui/vendor/addon-fit.js" "$APP/ui/vendor/addon-web-links.js"

rm -rf "$WORK"
PKG="$WORK/package"
mkdir -p "$PKG/cmd"

# 0. 强制可执行位 + 去除 UTF-8 BOM (Windows 下常见, 会破坏 shebang)
chmod +x "$APP/fcp" "$APP/webui.py"
strip_bom() {
    for f in "$@"; do
        [ -f "$f" ] || continue
        head -c 3 "$f" | od -An -tx1 | grep -q "ef bb bf" && sed -i '1s/^\xEF\xBB\xBF//' "$f" || true
    done
}
strip_bom "$APP/fcp" "$APP/webui.py" "$FNOS"/cmd/* "$FNOS/manifest" "$FNOS"/config/* "$FNOS"/wizard/* "$FNOS/ui/config" "$FNOS/Fncp.sc"

# 0.5 将桌面 UI 配置 (ui/config + images) 同步进 app/ui, 兼容 fnOS 从应用解压目录读图标配置
mkdir -p "$APP/ui/images"
cp -f "$FNOS/ui/config" "$APP/ui/config"
cp -f "$FNOS/ui/images/256.png" "$FNOS/ui/images/64.png" "$APP/ui/images/"

# 1. 构建 app.tgz (--mode=0755 保证 fcp/webui.py 可执行)
tar -C "$APP" -czf "$WORK/app.tgz" --mode=0755 fcp webui.py ui
log "app.tgz built: $(wc -c < "$WORK/app.tgz") bytes"

cp "$WORK/app.tgz" "$PKG/app.tgz"

# 2. 框架 cmd 脚本
cp "$FNOS"/cmd/* "$PKG/cmd/"

# 3. config / wizard / ui
cp -a "$FNOS/config" "$PKG/"
cp -a "$FNOS/wizard" "$PKG/"
cp -a "$FNOS/ui" "$PKG/"

# 4. 端口转发配置 + 图标
cp "$FNOS/Fncp.sc" "$PKG/"
cp "$FNOS/ICON.PNG" "$PKG/"
cp "$FNOS/ICON_256.PNG" "$PKG/"

# 5. manifest + checksum
cp "$FNOS/manifest" "$PKG/manifest"
CHECKSUM=$(md5sum "$WORK/app.tgz" | cut -d' ' -f1)
sed -i "s/^checksum.*/checksum        = ${CHECKSUM}/" "$PKG/manifest"
log "manifest patched: checksum=${CHECKSUM}"

APPNAME=$(grep '^appname' "$PKG/manifest" | awk -F= '{print $2}' | tr -d ' ')
VERSION=$(grep '^version' "$PKG/manifest" | awk -F= '{print $2}' | tr -d ' ')
PLATFORM=$(grep '^platform' "$PKG/manifest" | awk -F= '{print $2}' | tr -d ' ')

# 6. 打包 .fpk (--mode=0755 强制可执行位, 兼容 Windows 下 chmod 失效)
mkdir -p "$DIST"
FPK="$DIST/${APPNAME}_${VERSION}_${PLATFORM}.fpk"
rm -f "$FPK"
(cd "$PKG" && tar -czf "$FPK" --mode=0755 *)
rm -rf "$WORK"

log "BUILT: $FPK ($(wc -c < "$FPK") bytes)"
