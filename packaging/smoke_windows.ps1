# packaging/smoke_windows.ps1
#
# 在原生 Windows 主机上验证安装器的最小闭环：安装、启动冻结版服务、检查
# 产品专属健康响应与内置 skill runtime，最后卸载。脚本只使用临时目录，不读取
# 或修改开发数据库，也不需要模型凭据。

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$InstallerPath,

  [int]$Port = 0,

  [string]$ExpectedPydanticCoreAbi = "",

  [switch]$KeepArtifacts
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Assert-ExitCode {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Step,

    [Parameter(Mandatory = $true)]
    [int]$ExitCode
  )

  if ($ExitCode -ne 0) {
    throw "$Step failed with exit code $ExitCode."
  }
}

function Get-FreeLoopbackPort {
  param(
    [int]$RequestedPort
  )

  if ($RequestedPort -gt 0) {
    return $RequestedPort
  }

  $start = Get-Random -Minimum 52000 -Maximum 52900
  for ($candidate = $start; $candidate -lt 53000; $candidate++) {
    $probe = $null
    try {
      $probe = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        $candidate
      )
      $probe.Start()
      return $candidate
    } catch [System.Net.Sockets.SocketException] {
      # 端口被占用时继续探测下一个候选端口。
    } finally {
      if ($null -ne $probe) {
        $probe.Stop()
      }
    }
  }

  throw "Could not find a free loopback port in 52000-52999."
}

function Invoke-Installer {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,

    [Parameter(Mandatory = $true)]
    [string]$InstallDirectory
  )

  $arguments = @(
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/CLOSEAPPLICATIONS",
    "/DIR=`"$InstallDirectory`""
  )
  $result = Start-Process -FilePath $Path -ArgumentList $arguments -Wait -PassThru
  Assert-ExitCode "Windows installer" $result.ExitCode
}

function Invoke-Uninstaller {
  param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDirectory
  )

  $uninstaller = Get-ChildItem -LiteralPath $InstallDirectory -Filter "unins*.exe" -File |
    Select-Object -First 1
  if ($null -eq $uninstaller) {
    throw "Installed uninstaller was not found in $InstallDirectory."
  }

  $arguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
  $result = Start-Process -FilePath $uninstaller.FullName -ArgumentList $arguments -Wait -PassThru
  Assert-ExitCode "Windows uninstaller" $result.ExitCode
}

function Invoke-RuntimeProbe {
  param(
    [Parameter(Mandatory = $true)]
    [string]$RuntimePython,

    [Parameter(Mandatory = $true)]
    [string]$ProbeDirectory
  )

  $stdoutPath = Join-Path $ProbeDirectory "runtime.stdout.log"
  $stderrPath = Join-Path $ProbeDirectory "runtime.stderr.log"
  $probeCode = "import ssl, requests, docx, openpyxl, pypdf, PIL; print('runtime-ok')"
  Push-Location (Split-Path -Parent $RuntimePython)
  try {
    $probeOutput = & $RuntimePython -c $probeCode 2>&1
    $probeExitCode = $LASTEXITCODE
    $probeOutput = $probeOutput | Out-String
  } finally {
    Pop-Location
  }
  $probeOutput | Out-File -FilePath $stdoutPath -Encoding utf8
  if ($probeExitCode -ne 0) {
    $probeOutput | Out-File -FilePath $stderrPath -Encoding utf8
  }
  Assert-ExitCode "Bundled Python runtime probe" $probeExitCode
  if ($probeOutput -notmatch "runtime-ok") {
    throw "Bundled Python runtime probe did not emit its success marker."
  }
}

function Assert-InstalledPydanticCore {
  param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDirectory,

    [string]$ExpectedAbiToken = ""
  )

  $allNativeModules = @(Get-ChildItem -LiteralPath $InstallDirectory -Recurse -File -Filter "_pydantic_core*.pyd")
  $expectedDirectories = @(
    [IO.Path]::GetFullPath((Join-Path $InstallDirectory "pydantic_core")).TrimEnd('\'),
    [IO.Path]::GetFullPath((Join-Path $InstallDirectory "_internal\pydantic_core")).TrimEnd('\')
  )
  $misplacedModules = @($allNativeModules | Where-Object {
    $moduleDirectory = [IO.Path]::GetFullPath($_.Directory.FullName).TrimEnd('\')
    $expectedDirectories -notcontains $moduleDirectory
  })
  if ($misplacedModules.Count -gt 0) {
    $paths = ($misplacedModules | ForEach-Object { $_.FullName }) -join "; "
    throw "Installed package contains pydantic_core native extensions outside the pydantic_core package directory: $paths"
  }
  $nativeModules = @($allNativeModules | Where-Object {
    $moduleDirectory = [IO.Path]::GetFullPath($_.Directory.FullName).TrimEnd('\')
    $_.Directory.Name -ieq "pydantic_core" -and $expectedDirectories -contains $moduleDirectory
  })
  if ($nativeModules.Count -eq 0) {
    throw "Installed package is missing pydantic_core\_pydantic_core*.pyd under the pydantic_core package directory."
  }
  if ($nativeModules.Count -ne 1) {
    $names = ($nativeModules | ForEach-Object { $_.Name }) -join ", "
    throw "Installed package contains multiple pydantic_core native extensions; expected exactly one file, found: $names"
  }
  foreach ($nativeModule in $nativeModules) {
    if ($ExpectedAbiToken -and
        $nativeModule.Name -notmatch "\.$([regex]::Escape($ExpectedAbiToken))[-.]" -and
        $nativeModule.Name -notmatch "\.abi3[-.]" ) {
      throw "Installed package Pydantic core ABI mismatch: expected $ExpectedAbiToken, found $($nativeModule.Name)."
    }
    Write-Host "OK: installed pydantic core native extension path: $($nativeModule.FullName)"
  }
  Write-Host "OK: installed pydantic core native extension found ($($nativeModules.Count) file(s))"
}

if (-not [Environment]::Is64BitOperatingSystem) {
  throw "The Windows package is x64-only; a 64-bit Windows OS is required."
}

$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) (
  "gongge-xuban-windows-smoke-" + [Guid]::NewGuid().ToString("N")
)
$installDirectory = Join-Path $tempRoot "install"
$dataDirectory = Join-Path $tempRoot "data"
$stdoutPath = Join-Path $tempRoot "launcher.stdout.log"
$stderrPath = Join-Path $tempRoot "launcher.stderr.log"
$process = $null
$success = $false
$environmentNames = @(
  "GONGGE_XUBAN_HEADLESS",
  "GONGGE_XUBAN_DATA_DIR",
  "GONGGE_XUBAN_PORT",
  "GONGGE_XUBAN_PORT_RANGE_START",
  "GONGGE_XUBAN_PORT_RANGE_END",
  "GONGGE_XUBAN_DOTENV",
  "PUBLIC_MOCK_API_KEY"
)
$previousEnvironment = @{}

New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
$port = Get-FreeLoopbackPort $Port

try {
  foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
  }

  Write-Host "==> Installing Windows package into a temporary directory"
  Invoke-Installer $installer $installDirectory

  $executable = Join-Path $installDirectory "gongge-xuban.exe"
  if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Installed executable was not found: $executable"
  }
  Assert-InstalledPydanticCore $installDirectory $ExpectedPydanticCoreAbi

  $runtimePython = Join-Path $installDirectory "runtime\python.exe"
  if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    throw "Bundled Windows skill runtime was not found: $runtimePython"
  }
  Invoke-RuntimeProbe $runtimePython $tempRoot

  [Environment]::SetEnvironmentVariable("GONGGE_XUBAN_HEADLESS", "1", "Process")
  [Environment]::SetEnvironmentVariable("GONGGE_XUBAN_DATA_DIR", $dataDirectory, "Process")
  [Environment]::SetEnvironmentVariable("GONGGE_XUBAN_PORT", "$port", "Process")
  [Environment]::SetEnvironmentVariable("GONGGE_XUBAN_PORT_RANGE_START", "$port", "Process")
  [Environment]::SetEnvironmentVariable("GONGGE_XUBAN_PORT_RANGE_END", "$port", "Process")
  # 不让宿主机自定义 dotenv 改变默认桌面配置路径；用户配置由安装后的用户数据目录承载。
  [Environment]::SetEnvironmentVariable("GONGGE_XUBAN_DOTENV", $null, "Process")
  # 让冻结版验证自身的无 .env 默认配置路径，不把宿主机真实 mock key 带入测试。
  [Environment]::SetEnvironmentVariable("PUBLIC_MOCK_API_KEY", $null, "Process")

  Write-Host "==> Starting installed frozen executable on loopback port $port"
  $processParameters = @{
    FilePath = $executable
    WorkingDirectory = $installDirectory
    RedirectStandardOutput = $stdoutPath
    RedirectStandardError = $stderrPath
    PassThru = $true
  }
  $process = Start-Process @processParameters

  $health = $null
  for ($attempt = 1; $attempt -le 90; $attempt++) {
    $process.Refresh()
    if ($process.HasExited) {
      throw "Installed executable exited before health became ready (code $($process.ExitCode)). Diagnostics: $tempRoot; stdout=$stdoutPath; stderr=$stderrPath; runtimeLog=$(Join-Path $dataDirectory 'logs\gongge-xuban.log')"
    }
    try {
      $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 3
      if ($health.status -eq "ok" -and $health.product_id -eq "gongge-xuban") {
        break
      }
    } catch {
      # 服务尚未监听，继续等待；最终超时会失败并保留诊断目录。
    }
    Start-Sleep -Seconds 1
  }
  if ($null -eq $health -or $health.status -ne "ok" -or $health.product_id -ne "gongge-xuban") {
    throw "Installed executable did not expose the expected product health response. Diagnostics: $tempRoot; stdout=$stdoutPath; stderr=$stderrPath; runtimeLog=$(Join-Path $dataDirectory 'logs\gongge-xuban.log')"
  }

  $chat = Invoke-WebRequest -Uri "http://127.0.0.1:$port/chat/" -UseBasicParsing -TimeoutSec 5
  if ($chat.StatusCode -ne 200) {
    throw "Installed frontend route returned HTTP $($chat.StatusCode), expected 200."
  }

  $runtimeLog = Join-Path $dataDirectory "logs\gongge-xuban.log"
  if (-not (Test-Path -LiteralPath $runtimeLog -PathType Leaf)) {
    throw "Frozen runtime did not create its user-data log: $runtimeLog"
  }
  Write-Host "OK: installed executable health, frontend route, runtime and logging passed"

  if ($null -ne $process -and -not $process.HasExited) {
    Stop-Process -Id $process.Id -Force
    $process.WaitForExit()
  }
  $process = $null

  Write-Host "==> Uninstalling the temporary Windows package"
  Invoke-Uninstaller $installDirectory
  if (Test-Path -LiteralPath $executable -PathType Leaf) {
    throw "Windows uninstaller left the application executable behind."
  }

  $success = $true
  Write-Host "WINDOWS_NATIVE_SMOKE_PASS"
} finally {
  if ($null -ne $process -and -not $process.HasExited) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    $process.WaitForExit()
  }

  foreach ($name in $environmentNames) {
    [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process")
  }

  if ($success -and -not $KeepArtifacts) {
    if (Test-Path -LiteralPath $tempRoot) {
      Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
  } elseif (Test-Path -LiteralPath $tempRoot) {
    Write-Warning "Windows smoke diagnostics retained at $tempRoot"
  }
}
