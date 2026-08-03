#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
VERSION="${VERSION:-0.1.0}"
ARCH="$(uname -m)"
MAC_SIGN_ID="${MAC_SIGN_ID:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"
NOTARY_KEYCHAIN="${NOTARY_KEYCHAIN:-}"

sign_code() {
  local target="$1"
  if [ -n "$MAC_SIGN_ID" ]; then
    codesign --force --timestamp --options runtime --sign "$MAC_SIGN_ID" "$target"
  else
    codesign --force --timestamp=none --sign - "$target" 2>/dev/null || true
  fi
}

sign_app_bundle() {
  local app="$1"
  xattr -cr "$app" 2>/dev/null || true
  if [ -n "$MAC_SIGN_ID" ]; then
    echo "使用 Developer ID 签名"
    python3 - "$app" <<'PY' | while IFS= read -r item; do
import subprocess
import sys
from pathlib import Path

app = Path(sys.argv[1])
items = []
for path in app.rglob("*"):
    if not path.is_file() or path.is_symlink():
        continue
    try:
        desc = subprocess.check_output(["file", str(path)], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        continue
    if "Mach-O" in desc:
        items.append(path)

for path in sorted(items, key=lambda p: len(p.parts), reverse=True):
    print(path)
PY
      sign_code "$item"
    done
  else
    echo "未配置 MAC_SIGN_ID，使用 ad-hoc 签名"
    find "$app/Contents/Frameworks" -type f -name "*.dylib" 2>/dev/null \
      -exec codesign --force --timestamp=none --sign - {} \; 2>/dev/null || true
    codesign --force --timestamp=none --sign - "$app/Contents/MacOS/gongge-xuban" 2>/dev/null || true
  fi
  sign_code "$app"
}

echo "==> [1/5] 构建前端"
npm --prefix frontend-enterprise run build

echo "==> [2/5] 后端 venv + 运行依赖 + 打包工具"
cd backend
if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
  .venv/bin/python -m ensurepip --upgrade 2>/dev/null || true
fi
# 每次打包都按 pyproject 重新解析依赖。只检查 pyinstaller 是否存在会让旧 venv
# 绕过 cryptography/OpenSSL 的兼容上限，产出无法启动但仍可完成签名的 Intel 包。
DEPS="$(.venv/bin/python -c "import tomllib,pathlib; print(' '.join(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']))")"
if .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m pip install -U pip
  # DEPS 来自仓库内 pyproject 的依赖数组，需要按 shell 参数拆分。
  # shellcheck disable=SC2086
  .venv/bin/python -m pip install $DEPS "pyinstaller>=6.6.0" "certifi>=2024.2.2"
elif command -v uv >/dev/null 2>&1; then
  # shellcheck disable=SC2086
  VIRTUAL_ENV="$(pwd)/.venv" uv pip install $DEPS "pyinstaller>=6.6.0" "certifi>=2024.2.2"
else
  echo "无法安装打包依赖：venv 既无 pip 也无 uv" >&2
  exit 1
fi
# macOS Dock 壳依赖 pyobjc（幂等，已装则跳过）
if ! .venv/bin/python -c "import AppKit" >/dev/null 2>&1; then
  if .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m pip install "pyobjc-framework-Cocoa>=10.0"
  elif command -v uv >/dev/null 2>&1; then
    VIRTUAL_ENV="$(pwd)/.venv" uv pip install "pyobjc-framework-Cocoa>=10.0"
  fi
fi

echo "==> [3/5] PyInstaller 打包（spec 在 macOS 下同时产出 Gongge-Xuban.app）"
.venv/bin/pyinstaller ../packaging/gongge-xuban.spec --noconfirm \
  --distpath ../packaging/out --workpath ../packaging/build
cd "$REPO"
APP="packaging/out/Gongge-Xuban.app"
test -d "$APP" || { echo "PyInstaller 未产出 $APP"; exit 1; }

echo "==> [4/5] 附带 python 运行时（放 .app/Contents/Resources/runtime）"
# 注意：runtime 必须放 Resources 而非 MacOS。放 MacOS 时 codesign 会把 runtime 里
# 每个文件都当作需签名的代码，附带 python 有大量脚本/符号链接/畸形目录（如 itcl4.2.2），
# 导致顶层签名失败、密封无效（"a sealed resource is missing or invalid"）→ 无法双击打开。
# 放 Resources 后按数据资源密封，顶层签名可通过，app 能正常启动。
python3 packaging/fetch_runtime_python.py packaging/runtime_dl --expect-arch "$ARCH"
rm -rf "$APP/Contents/Resources/runtime" "$APP/Contents/MacOS/runtime"
cp -R packaging/runtime_dl/python "$APP/Contents/Resources/runtime"

echo "==> [5/5] 签名 + 打 dmg"
sign_app_bundle "$APP"

if codesign --verify --deep --strict "$APP" 2>/dev/null; then
  echo "✓ 签名密封验证通过"
else
  echo "警告：密封校验未过，双击可能无法打开"
fi

# 在构建 runner 的原生架构上启动已签名 App。Intel runner 可在制成 DMG 前捕获
# cryptography/OpenSSL ABI 冲突，arm64 runner 同时验证自身产物的完整启动链。
bash packaging/smoke_macos_app.sh "$APP"

DMG="packaging/out/Gongge-Xuban-macos-${ARCH}.dmg"
DMG_ROOT="packaging/out/dmg-root"
DMG_BACKGROUND="packaging/build/gongge-xuban-dmg-background.png"
rm -f "$DMG"
rm -f "packaging/out/rw."*"Gongge-Xuban-macos-${ARCH}.dmg" 2>/dev/null || true
rm -rf "$DMG_ROOT"
mkdir -p "$DMG_ROOT"
ditto "$APP" "$DMG_ROOT/Gongge-Xuban.app"
python3 packaging/make_dmg_background.py "$DMG_BACKGROUND"

if command -v create-dmg >/dev/null 2>&1; then
  LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 create-dmg --volname "Gongge-Xuban" \
    --window-pos 120 100 --window-size 840 360 \
    --background "$DMG_BACKGROUND" \
    --icon-size 96 --text-size 13 \
    --icon "Gongge-Xuban.app" 230 180 \
    --hide-extension "Gongge-Xuban.app" \
    --app-drop-link 610 175 \
    --app-drop-link-name "Applications" \
    --volicon "packaging/assets/gongge-xuban.icns" \
    --no-internet-enable --overwrite \
    "$DMG" "$DMG_ROOT" \
    || { ln -s /Applications "$DMG_ROOT/Applications"; hdiutil create -volname Gongge-Xuban -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG"; }
else
  ln -s /Applications "$DMG_ROOT/Applications"
  hdiutil create -volname Gongge-Xuban -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG"
fi
rm -rf "$DMG_ROOT"
rm -f "packaging/out/rw."*"Gongge-Xuban-macos-${ARCH}.dmg" 2>/dev/null || true

if [ -n "$MAC_SIGN_ID" ]; then
  codesign --force --timestamp --sign "$MAC_SIGN_ID" "$DMG"
  codesign --verify --strict "$DMG"
fi

if [ -n "$NOTARY_PROFILE" ]; then
  if [ -z "$MAC_SIGN_ID" ]; then
    echo "配置 NOTARY_PROFILE 时也必须配置 MAC_SIGN_ID" >&2
    exit 1
  fi
  NOTARY_ARGS=(--keychain-profile "$NOTARY_PROFILE")
  if [ -n "$NOTARY_KEYCHAIN" ]; then
    NOTARY_ARGS+=(--keychain "$NOTARY_KEYCHAIN")
  fi
  xcrun notarytool submit "$DMG" "${NOTARY_ARGS[@]}" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
  spctl -a -vvv -t open --context context:primary-signature "$DMG"
fi

echo "built $DMG"
ls -lh "$DMG"
