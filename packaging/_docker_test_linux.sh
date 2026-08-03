#!/usr/bin/env bash
# 在 x86_64 Ubuntu 容器里测试 build_linux.sh
# 项目挂载到 /work（可写），输出在 /work/packaging/out
set -e

echo "=========================================="
echo "  容器架构: $(uname -m)"
echo "=========================================="

echo "==> 装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  curl wget file software-properties-common \
  ruby ruby-dev build-essential \
  libfuse2 fuse \
  ca-certificates \
  >/dev/null 2>&1 || { echo "apt 装依赖失败"; exit 1; }

echo "==> 装 Python 3.11（项目要求 >=3.11，ubuntu:22.04 自带 3.10）"
add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv python3.11-dev >/dev/null 2>&1
echo "python3.11: $(python3.11 --version 2>&1)"

echo "==> 装 node 20"
curl -fsSL https://deb.nodesource.com/setup_20.x 2>/dev/null | bash - >/dev/null 2>&1
apt-get install -y -qq nodejs >/dev/null 2>&1
echo "node: $(node -v 2>&1), npm: $(npm -v 2>&1)"

echo "==> 装 fpm"
gem install --no-document fpm >/dev/null 2>&1 && echo "fpm: $(fpm --version 2>&1)" || echo "fpm 装失败"

echo "==> 下载 appimagetool（QEMU 模拟无 FUSE，需解压成普通可执行）"
cd /tmp
wget -q -O appimagetool.AppImage \
  https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x appimagetool.AppImage
# QEMU 模拟环境 FUSE 不可用，AppImage 自身跑不了 → 解压出来直接用其内部的 AppRun
./appimagetool.AppImage --appimage-extract >/dev/null 2>&1 || true
if [ -x /tmp/squashfs-root/AppRun ]; then
  # 用解压后的 appimagetool
  cat > /usr/local/bin/appimagetool <<'WRAP'
#!/bin/sh
exec /tmp/squashfs-root/AppRun "$@"
WRAP
  chmod +x /usr/local/bin/appimagetool
  echo "appimagetool 已解压可用"
else
  cp appimagetool.AppImage /usr/local/bin/appimagetool
  chmod +x /usr/local/bin/appimagetool
  echo "appimagetool 解压失败，用原 AppImage（可能因 FUSE 失败）"
fi
cd /work

echo "==> python: $(python3 --version 2>&1)"

echo ""
echo "=========================================="
echo "  环境就绪，把项目拷到容器内部（避开挂载 macOS fs 的符号链接问题）"
echo "=========================================="
# 关键：不在挂载的 /work 里构建（macOS fs 对 python 符号链接处理有问题）。
# 把源码拷到容器内部 /build，在那里构建，产物再拷回 /work/packaging/out。
rm -rf /build && mkdir -p /build
# 拷源码，排除大目录和 arm64 产物
apt-get install -y -qq rsync >/dev/null 2>&1
rsync -a --exclude='.git' --exclude='backend/.venv' \
  --exclude='frontend-enterprise/node_modules' \
  --exclude='packaging/out' --exclude='packaging/build' \
  --exclude='packaging/runtime_dl' --exclude='frontend-enterprise/dist' \
  --exclude='.dev' \
  /work/ /build/
cd /build

# 前端 node_modules 是 macOS(arm64) 装的，容器里重装并构建
echo "==> 容器内重装前端依赖并构建"
( cd frontend-enterprise && npm install >/dev/null 2>&1 && npm run build )
echo "前端 dist 已就绪（x86_64 容器内构建）"

# appimagetool、版本、python 命令、pip 镜像
export APPIMAGETOOL=/usr/local/bin/appimagetool
export VERSION=0.1.0
ln -sf /usr/bin/python3.11 /usr/local/bin/python 2>/dev/null || true
echo "python -> $(python --version 2>&1)"
mkdir -p /etc/pip
cat > /etc/pip.conf <<'PIPCONF'
[global]
index-url = https://mirrors.aliyun.com/pypi/simple/
timeout = 120
retries = 5
PIPCONF

echo ""
echo "=========================================="
echo "  在容器内部跑 build_linux.sh（前端已 build）"
echo "=========================================="
# 允许 build_linux.sh 部分失败（AppImage 在 QEMU 模拟无 FUSE 可能失败），不中断后续拷回
set +e
SKIP_FRONTEND=1 bash packaging/build_linux.sh
BUILD_RC=$?
set -e
echo "build_linux.sh 退出码: $BUILD_RC"

echo ""
echo "==> 把产物拷回挂载目录 /work/packaging/out"
mkdir -p /work/packaging/out
cp -f packaging/out/Gongge-Xuban-linux-x86_64.deb /work/packaging/out/ 2>/dev/null && echo "✓ deb 已拷回" || echo "✗ 无 deb"
cp -f packaging/out/Gongge-Xuban-linux-x86_64.AppImage /work/packaging/out/ 2>/dev/null && echo "✓ AppImage 已拷回" || echo "✗ 无 AppImage（模拟环境 FUSE 限制，真机可出）"
ls -lh /work/packaging/out/Gongge-Xuban-linux-x86_64.* 2>/dev/null || echo "（无 Linux 产物）"
