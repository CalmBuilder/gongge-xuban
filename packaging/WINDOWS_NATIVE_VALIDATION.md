# Windows/native 验收

Windows 版本必须在原生 Windows x64 主机上构建和验收。Linux、WSL、Wine 或
PowerShell Core 的语法检查只能证明脚本可解析，不能替代 PyInstaller 的
Windows PE 产物、Inno Setup 安装行为、Windows 用户目录和 taskbar 壳的原生验证。

## 原生构建

在仓库根目录打开 Windows PowerShell，准备以下工具：

1. Python 3.11+（x64）和 Node.js；
2. Inno Setup 6 或 7，并让 `ISCC.exe` 位于脚本可识别的默认目录，或通过 `ISCC` 环境变量指定；
   官方下载页为 <https://jrsoftware.org/isdl.php>，也可使用
   `winget install --id JRSoftware.InnoSetup -e -s winget -i`；
3. Windows SDK（仅公开发布或配置签名时需要 `signtool.exe`）；
4. 可访问 GitHub release 和 Python/npm 包源的网络。

然后执行：

```powershell
$env:VERSION = "0.1.0"
# 如果 Inno Setup 安装在自定义目录，请先指定 ISCC.exe，例如：
# $env:ISCC = "C:\Inno Setup 7\ISCC.exe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

上面的变量赋值是 PowerShell 语法。不要把 `$env:VERSION = ...` 直接粘贴到
`cmd.exe`；如果当前窗口提示“文件名、目录名或卷标语法不正确”，请打开 Windows
PowerShell 后重试，或者在 cmd 中先执行 `set VERSION=0.1.0`，再调用
`powershell.exe -File packaging\build_windows.ps1`。

构建脚本会先校验宿主机和 Python 均为 x64，并在 PyInstaller 前停止同名的旧版
`gongge-xuban` 进程、清理旧的产品输出目录和 `packaging\build` 工作目录；随后按
`backend/pyproject.toml` 安装依赖，使用
PyInstaller 的 `--clean` 生成并校验 PE machine=`0x8664` 的 x64 可执行文件，最后生成
`packaging\out\Gongge-Xuban-windows-x64-setup.exe`，并自动调用
`packaging\smoke_windows.ps1`。未配置证书时只允许本地 unsigned 验证，脚本会明确
打印 `UNSIGNED`；公开发布必须配置 Authenticode 证书并通过签名校验。构建阶段还会显式
收集并验证 `pydantic_core\_pydantic_core*.pyd`，安装后的 Smoke 测试会再次确认该扩展
存在后才启动冻结版服务。

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

桌面启动默认优先使用 `127.0.0.1:5137`；Windows 冻结版只使用两个确定候选端口：
5137 和专用回退端口 59137。如果 5137 已被其他进程占用，会尝试 59137；两者都被
占用时明确失败，不会覆盖或强杀占用者。开发态和 macOS 仍可通过
`GONGGE_XUBAN_PORT_RANGE_START` 到 `GONGGE_XUBAN_PORT_RANGE_END` 使用端口范围。
这不是随机改端口，也不会把 5137 变成 5138。
冻结版日志会记录 `port_selected`、`app_preloaded`、数据库初始化和演示数据同步的耗时，
日志位置为 `%APPDATA%\Gongge-Xuban\logs\gongge-xuban.log`，可据此区分端口探测、Python
导入和数据库阶段的启动延迟。

也可以对已有安装器单独运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File packaging\smoke_windows.ps1 `
  -InstallerPath packaging\out\Gongge-Xuban-windows-x64-setup.exe
```

这条自动门禁使用 headless 服务，不把默认浏览器弹窗误判为服务健康。发布前仍需在
实际 Windows 桌面完成一次人工浏览器验收：双击快捷方式，确认默认浏览器打开
`/chat/`、taskbar 图标显示为“共格·序伴”；再次点击 taskbar 图标或用 Alt+Tab 选择
该图标时，应置前已有的共格浏览器窗口，不得新建标签页或第二个服务进程。仅当浏览器
窗口确实已关闭时，才允许重新打开一个页面；退出后端口释放且重新启动可恢复。Windows
冻结版通过命名互斥锁先完成单实例裁决，启动中的首实例尚未健康时，后续启动会等待并
复用它。

安装器启用 `CloseApplications=yes`，更新安装时只请求关闭占用共格·序伴安装文件的
应用；不会按端口号强杀未知进程。

## 已知问题处理

下面记录构建和原生验收中实际遇到过的问题。处理原则是修复源码或构建契约后重新
构建，不通过手工修改 `packaging\out`、放宽架构检查或强杀未知进程来“通过”验收。

### 前端构建找不到 `gsap`

如果出现 `src/lib/gsap.ts: Cannot find module 'gsap'`，通常是依赖声明或锁文件未随
当前提交同步，不能只在本机遗留的 `node_modules` 中补包。先确认已拉取包含依赖声明的
最新提交，然后清理并按锁文件安装：

```powershell
git pull --ff-only origin main
npm ci --prefix frontend-enterprise --no-audit --no-fund
npm --prefix frontend-enterprise run build
```

构建脚本本身已经执行 `npm ci`；若仍失败，应检查 `frontend-enterprise/package.json`
和 `package-lock.json` 是否同时包含 `gsap`，修复后再运行完整构建。

### 冻结程序提示 `public_mock_api_key` 缺失

这是旧版冻结包在没有 `.env` 时未满足本地 mock API 配置契约的表现。当前版本在冻结
启动早期生成进程级临时 key，并把用户配置放在 `%APPDATA%\Gongge-Xuban\.env`；不会把
真实 API key 写进源码或安装包。遇到该堆栈时不要把密钥写入仓库，直接拉取最新代码并
重新构建；仍失败时保留启动器 stdout/stderr 及用户日志供定位。

### PE 架构检查误报 `t32.exe` 或 `t64.exe`

`pip\_vendor\distlib` 和 setuptools 中的 `t32/t64`、`cli/gui` 是生成入口脚本的
多架构模板，并非应用运行时。构建脚本只对这些确定路径记录“跳过”，其余 `.exe`、`.dll`、
`.pyd`、`.node` 仍必须是 AMD64 (`0x8664`)。出现其他路径的 `0x014C` 或 ARM 架构时，
必须修复 Python/依赖来源或打包配置，不能把整个 PE 检查关闭。

### 重复进程、重复标签页或 Alt+Tab 再开页面

构建前脚本只停止进程名恰为 `gongge-xuban` 的旧产品进程；运行时通过 Windows 命名
互斥锁保证单实例，并将 taskbar/Alt+Tab 操作聚焦到已有的共格浏览器窗口。人工验收
看到重复实例时，先退出旧安装或仅结束确认过的 `gongge-xuban.exe`，再检查：

- 再次点击快捷方式不应产生第二个服务进程；
- 再次点击 taskbar 或 Alt+Tab 应置前已有页面，不应新建标签页；
- 关闭浏览器后再次点击，才允许重新打开一个页面。

不要通过结束所有同名或所有浏览器进程来排障。

### 启动变慢或出现 5138 端口

旧版冻结包会扫描 `5137-5199`，所以可能落到 `5138`；当前 Windows 冻结版只按
`5137 → 59137` 两个确定候选端口运行。确认测试的是最新安装包，并查看：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 5137,59137 |
  Select-Object LocalAddress,LocalPort,OwningProcess
Get-Content "$env:APPDATA\Gongge-Xuban\logs\gongge-xuban.log" -Tail 100
```

日志中的 `port_selected`、`app_preloaded`、`database_initialized`、`demo_data_seeded`
和 `workers_started` 可区分端口探测、Python 导入和数据库阶段的耗时。5137 被非共格
服务占用时使用 59137；已有健康的共格服务则复用；两个端口都占用时应明确失败，不能
按端口号盲目 `kill`。只有确认 PID 的进程名和路径均为共格·序伴时，才可人工结束该
产品旧进程后重试。

### 安装或卸载失败

构建输出目录被运行中的产品锁定时，退出共格·序伴后重新运行构建脚本。安装器的
`CloseApplications=yes` 只处理占用共格安装文件的应用，不负责关闭端口上的第三方服务。
冒烟失败时保留脚本提示的临时诊断目录（使用 `-KeepArtifacts`），重点检查
`launcher.stdout.log`、`launcher.stderr.log` 和运行日志；不要手工删除正在使用的
安装目录或数据库文件。

### Inno Setup 下载、安装或 `ISCC.exe` 识别失败

构建脚本不是从网络自动下载 Inno Setup，而是查找本机的 `ISCC.exe`。官方下载页的
下载链接可能跳转到 GitHub；如果浏览器打不开、下载被代理或安全软件拦截，可在能访问
包源的 Windows 主机使用官方页面提供的 `winget` 安装方式，或在另一台可信机器下载后
通过受控介质传输并校验签名。不要从不明镜像下载编译器。

安装后若构建仍提示 `Inno Setup ISCC.exe was not found`，先确认编译器存在，再把完整路径传给
脚本支持的 `ISCC` 环境变量。脚本会优先使用当前 PowerShell 会话中的 `$env:ISCC`，因此
自定义目录（例如 `C:\Inno Setup 7`）应显式设置为其中的 `ISCC.exe`：

```powershell
Get-ChildItem "$env:ProgramFiles", "${env:ProgramFiles(x86)}", "$env:LOCALAPPDATA\Programs" `
  -Filter ISCC.exe -Recurse -ErrorAction SilentlyContinue |
  Select-Object -First 5 -ExpandProperty FullName

$env:ISCC = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
# Inno Setup 7 的自定义安装目录示例：
# $env:ISCC = "C:\Inno Setup 7\ISCC.exe"
if (-not (Test-Path -LiteralPath $env:ISCC)) {
  throw "ISCC.exe was not found at $env:ISCC"
}
& $env:ISCC /?
```

如果安装的是 Inno Setup 7，脚本会优先自动发现标准目录和磁盘根目录下的 `Inno Setup *` 目录，
也不会从网络自动下载；自定义目录仍请将 `$env:ISCC` 指向实际的 `ISCC.exe` 后再构建，并先用同一版本完成一次本地冒烟。若不确定
文件位置，可执行：

```powershell
Get-ChildItem "C:\Inno Setup 7", "$env:ProgramFiles", "${env:ProgramFiles(x86)}", `
  "$env:LOCALAPPDATA\Programs" -Filter ISCC.exe -Recurse -ErrorAction SilentlyContinue |
  Select-Object -First 5 -ExpandProperty FullName
```

若希望后续 PowerShell 会话自动识别自定义安装目录，可设置用户级环境变量；设置后需重新
打开 PowerShell：

```powershell
[Environment]::SetEnvironmentVariable("ISCC", "C:\Inno Setup 7\ISCC.exe", "User")
```

若安装器下载后无法运行，先在文件属性中核对数字签名和发布者，再处理 Windows SmartScreen
提示；不要通过关闭系统安全防护来绕过校验。

### 冻结版启动时报 `pydantic_core._pydantic_core` 缺失

`pydantic-core` 是 Pydantic v2 的 Rust 原生扩展，Windows wheel 中对应的文件通常是
`_pydantic_core.cp311-win_amd64.pyd` 或 `_pydantic_core.cp313-win_amd64.pyd`。开发环境中
`pydantic` 能正常导入，并不代表该 `.pyd` 已进入 PyInstaller 冻结包。项目依赖允许
`pydantic>=2.7.0`，重新创建环境时可能解析到不同的 Pydantic/PyInstaller 组合，因此旧版本
曾经成功不代表新版本仍会被隐式识别。

当前 spec 已显式收集 `pydantic_core` 的模块和原生扩展，并把扩展固定放到冻结包的
`pydantic_core` 包目录；冻结启动钩子还会按完整模块名
`pydantic_core._pydantic_core` 预加载它，避免“文件存在但包路径未被解释器解析”。构建后可在仓库根目录检查：

```powershell
Get-ChildItem .\packaging\out\gongge-xuban `
  -Recurse -Filter "_pydantic_core*.pyd"
```

注意：`cp313`/`cp314` 是 CPython 原生扩展的 ABI 标记；正常产物会保留一个与本次构建解释器
一致的标记。需要消除的是同一包中同时出现不同 ABI，或出现与当前解释器不匹配的扩展，不能
通过删名或改名来“修复”原生文件。输出必须只有一个，且必须直接位于 `pydantic_core` 目录下
（PyInstaller 6 的 onedir 布局通常是 `_internal\pydantic_core`）；文件名中的
`cp311`/`cp313`/`cp314` 必须与本次构建解释器一致，不能同时残留多个 Python ABI 的文件。
构建脚本会检查 Python ABI、在构建环境中实际执行
`import pydantic_core._pydantic_core`，并通过同一解释器的 `python -m PyInstaller` 构建，避免
复用旧 `backend\.venv` 或旧 PyInstaller 启动器造成错配。Windows 构建使用并在每轮清理独立的
`packaging\.windows-build-venv`，不会改写开发环境；最终输出和安装后的 Smoke 测试都会再次拒绝
错 ABI、重复或放错目录的扩展。Smoke 测试会打印完整路径，并在冻结启动前验证该目录；
若安装包日志仍失败，查看诊断目录中的 `data\logs\gongge-xuban.log`，其中会保留
`ModuleNotFoundError`/`ImportError` 的模块详情和扩展路径。只看到 `.pyd` 文件或 Inno Setup
编译成功，均不等于冻结版可导入。

如果看到 `Module use of python314.dll conflicts with this version of Python`，说明构建过程中
混用了不同 Python 小版本或不同环境的原生扩展。脚本会自动清理并重建独立的
`packaging\.windows-build-venv`；若目录被占用或无法重建，先退出正在运行的开发服务，再重新执行完整构建，
不要手工复制 `cp314`/`cp313` 文件。

没有任何输出时不要发布安装器，应使用当前提交重新执行完整构建。若文件存在但仍无法导入，
再检查其 Python ABI、PE machine 是否为 AMD64，以及依赖 DLL 是否齐全；不要从其他 Python
环境手工复制 `.pyd` 到安装目录。

如果 `ISCC.exe` 能单独执行但构建仍失败，确认是在仓库根目录运行
`packaging\build_windows.ps1`，并检查 `packaging\installer\gongge-xuban.iss` 的
`SetupIconFile`、`Source` 路径和 `VERSION` 是否存在。构建成功必须同时看到安装器文件
和 `WINDOWS_NATIVE_SMOKE_PASS`；仅看到 Inno Setup 的编译成功信息不能替代安装、启动、
健康检查和卸载验收。

## 发布 GitHub Release

原生构建和人工浏览器验收均通过后，安装包作为 Release Asset 上传，不提交到 Git 历史。
建议让 GitHub tag 与安装器版本一致，例如：

1. 在 Windows 拉取目标提交，执行构建并确认 `WINDOWS_NATIVE_SMOKE_PASS`；
2. 在仓库的 **Releases → Draft a new release** 中创建 tag `v0.1.0`，目标为 `main`；
3. 上传 `packaging\out\Gongge-Xuban-windows-x64-setup.exe`，标题使用“共格·序伴 v0.1.0”；
4. 发布后从干净 Windows 用户环境下载并安装一次，确认附件可下载、安装器版本和
   `/chat/` 浏览器验收结果与本次构建一致。

可选地生成并上传 SHA-256 校验文件：

```powershell
$exe = "packaging\out\Gongge-Xuban-windows-x64-setup.exe"
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash
"$hash  $(Split-Path $exe -Leaf)" | Set-Content "$exe.sha256" -Encoding ascii
```

未配置 Authenticode 证书时，Release 说明必须明确安装包为 `UNSIGNED`；不能把本地
冒烟通过表述为已签名或已完成 Windows 安全发布。

## 当前 Linux 主机的边界

当前项目工作主机为 Linux，且没有 Windows 内核、Wine、Inno Setup 或 Windows SDK。
因此本机只能完成 PowerShell AST、Shell/Python 静态检查及跨平台契约测试；在原生
Windows 主机执行上述构建和冒烟后，才能把 Windows/native 标记为验收通过。
