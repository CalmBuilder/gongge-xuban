# Windows/native 验收

Windows 版本必须在原生 Windows x64 主机上构建和验收。Linux、WSL、Wine 或
PowerShell Core 的语法检查只能证明脚本可解析，不能替代 PyInstaller 的
Windows PE 产物、Inno Setup 安装行为、Windows 用户目录和 taskbar 壳的原生验证。

## 原生构建

在仓库根目录打开 Windows PowerShell，准备以下工具：

1. Python 3.11+（x64）和 Node.js；
2. Inno Setup 6，并让 `ISCC.exe` 位于默认安装目录或通过 `ISCC` 指定；
3. Windows SDK（仅公开发布或配置签名时需要 `signtool.exe`）；
4. 可访问 GitHub release 和 Python/npm 包源的网络。

然后执行：

```powershell
$env:VERSION = "0.1.0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

构建脚本会先校验宿主机和 Python 均为 x64，并在 PyInstaller 前停止同名的旧版
`gongge-xuban` 进程、清理旧的产品输出目录；随后按 `backend/pyproject.toml` 安装依赖，使用
PyInstaller 的 `--clean` 生成并校验 PE machine=`0x8664` 的 x64 可执行文件，最后生成
`packaging\out\Gongge-Xuban-windows-x64-setup.exe`，并自动调用
`packaging\smoke_windows.ps1`。未配置证书时只允许本地 unsigned 验证，脚本会明确
打印 `UNSIGNED`；公开发布必须配置 Authenticode 证书并通过签名校验。

## 自动门禁

`smoke_windows.ps1` 使用临时安装目录和临时用户数据目录，验证：

- Inno Setup 安装器可静默安装，且安装目录含 `gongge-xuban.exe`；
- 安装包内置 `runtime\python.exe` 能导入 PDF、Office、图片解析依赖；
- 安装包内置的实际 exe、dll、pyd、node payload 均通过 PE x64（AMD64）架构检查；
  pip/distlib 与 setuptools 中仅用于生成入口脚本的多架构模板会明确记录为跳过，不能
  把这些模板误判成应用运行时；
- 冻结版进程以 headless 模式启动，在固定 loopback 端口返回
  `status=ok`、`product_id=gongge-xuban`；
- `/chat/` 前端路由返回 HTTP 200，并创建产品运行日志；
- 停止进程后卸载器能删除应用可执行文件；
- 全部检查通过才输出 `WINDOWS_NATIVE_SMOKE_PASS`。

也可以对已有安装器单独运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File packaging\smoke_windows.ps1 `
  -InstallerPath packaging\out\Gongge-Xuban-windows-x64-setup.exe
```

这条自动门禁使用 headless 服务，不把默认浏览器弹窗误判为服务健康。发布前仍需在
实际 Windows 桌面完成一次人工浏览器验收：双击快捷方式，确认默认浏览器打开
`/chat/`、taskbar 图标显示为“共格·序伴”，再次点击图标只复用首个服务实例并打开
同一页面，不会启动第二个服务进程；退出后端口释放且重新启动可恢复。Windows 冻结版
通过命名互斥锁先完成单实例裁决，启动中的首实例尚未健康时，后续启动会等待并复用它。

## 当前 Linux 主机的边界

当前项目工作主机为 Linux，且没有 Windows 内核、Wine、Inno Setup 或 Windows SDK。
因此本机只能完成 PowerShell AST、Shell/Python 静态检查及跨平台契约测试；在原生
Windows 主机执行上述构建和冒烟后，才能把 Windows/native 标记为验收通过。
