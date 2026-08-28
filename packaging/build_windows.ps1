# packaging/build_windows.ps1
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
if (-not $env:VERSION) { $env:VERSION = "0.1.0" }

function Test-SigningConfigured {
  return [bool]($env:WINDOWS_CERT_THUMBPRINT -or $env:WINDOWS_PFX_PATH)
}

function Assert-NativeCommandSucceeded {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Step
  )

  if ($LASTEXITCODE -ne 0) {
    throw "$Step failed with exit code $LASTEXITCODE."
  }
}

function Assert-WindowsX64Host {
  # 只允许原生 Windows x64；WSL、Wine、32 位 Windows 和 ARM64 均不生成本包。
  if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "Windows native build is required; this script is running on a non-Windows host."
  }
  if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Windows x64 is required; a 32-bit operating system is not supported."
  }

  $nativeArchitecture = $env:PROCESSOR_ARCHITEW6432
  if ([string]::IsNullOrWhiteSpace($nativeArchitecture)) {
    $nativeArchitecture = $env:PROCESSOR_ARCHITECTURE
  }
  if ([string]::IsNullOrWhiteSpace($nativeArchitecture)) {
    throw "Windows native architecture could not be determined."
  }
  $nativeArchitecture = $nativeArchitecture.Trim().ToUpperInvariant()
  if ($nativeArchitecture -notin @("AMD64", "X86_64")) {
    throw "Windows x64 is required; detected native architecture: $nativeArchitecture"
  }
  Write-Host "Windows native architecture: $nativeArchitecture"
}

function Assert-PythonX64 {
  param(
    [Parameter(Mandatory = $true)]
    [pscustomobject]$Python
  )

  & $Python.Command @($Python.PrefixArgs) -c `
    "import platform, struct; machine = platform.machine().lower(); assert struct.calcsize('P') == 8 and machine in ('amd64', 'x86_64'), f'expected x64 Python, got {machine}'; print(f'Python x64: {machine}')"
  Assert-NativeCommandSucceeded "Python x64 architecture check"
}

function Assert-PeX64 {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path
  )

  $resolved = (Resolve-Path -LiteralPath $Path).Path
  $stream = $null
  $reader = $null
  try {
    $stream = [System.IO.File]::OpenRead($resolved)
    $reader = [System.IO.BinaryReader]::new($stream)
    if ($reader.ReadUInt16() -ne 0x5A4D) {
      throw "PE architecture check failed: $resolved is not a Windows executable."
    }
    $stream.Seek(0x3C, [System.IO.SeekOrigin]::Begin) | Out-Null
    $peOffset = $reader.ReadInt32()
    if ($peOffset -lt 0 -or $peOffset -gt ($stream.Length - 6)) {
      throw "PE architecture check failed: invalid PE header offset in $resolved."
    }
    $stream.Seek($peOffset, [System.IO.SeekOrigin]::Begin) | Out-Null
    if ($reader.ReadUInt32() -ne 0x00004550) {
      throw "PE architecture check failed: $resolved has no PE signature."
    }
    $machine = $reader.ReadUInt16()
    if ($machine -ne 0x8664) {
      throw "PE architecture check failed: $resolved machine is 0x$('{0:X4}' -f $machine), expected AMD64 (0x8664)."
    }
    Write-Host "PE x64 verified: $resolved"
  }
  finally {
    if ($null -ne $reader) {
      $reader.Dispose()
    } elseif ($null -ne $stream) {
      $stream.Dispose()
    }
  }
}

function Test-NonRuntimeExecutableTemplate {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path
  )

  $normalized = $Path.Replace("/", "\")
  return (
    $normalized -match '\\Lib\\site-packages\\pip\\_vendor\\distlib\\[tw](?:32|64)(?:-arm)?\.exe$' -or
    $normalized -match '\\Lib\\site-packages\\setuptools\\(?:cli|gui)(?:-(?:32|64|arm64))?\.exe$'
  )
}

function Assert-BundlePeX64 {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Root
  )

  $signableExtensions = @(".exe", ".dll", ".pyd", ".node")
  $files = @(Get-ChildItem -LiteralPath $Root -Recurse -File |
    Where-Object { $signableExtensions -contains $_.Extension.ToLowerInvariant() })
  if ($files.Count -eq 0) {
    throw "PE architecture check found no Windows binary payloads under $Root."
  }
  $verifiedCount = 0
  $templateCount = 0
  foreach ($file in $files) {
    if ($file.Extension -eq ".exe" -and (Test-NonRuntimeExecutableTemplate $file.FullName)) {
      $templateCount += 1
      Write-Host "PE architecture check skipped known launcher template: $($file.FullName)"
      continue
    }
    Assert-PeX64 $file.FullName
    $verifiedCount += 1
  }
  if ($verifiedCount -eq 0) {
    throw "PE architecture check found no executable payloads to verify under $Root."
  }
  Write-Host "PE x64 payloads verified: $verifiedCount file(s); launcher templates skipped: $templateCount"
}

function Stop-ExistingProductProcesses {
  $running = @(Get-Process -Name "gongge-xuban" -ErrorAction SilentlyContinue)
  if ($running.Count -eq 0) {
    return
  }

  $processIds = @($running | Select-Object -ExpandProperty Id)
  Write-Host "==> stopping $($running.Count) running gongge-xuban process(es) holding the build output"
  $running | Stop-Process -Force
  Start-Sleep -Seconds 1
  foreach ($processId in $processIds) {
    if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
      throw "Could not stop gongge-xuban process $processId; the build output may be locked."
    }
  }
}

function Clear-ApplicationBuildOutput {
  $applicationOutput = Join-Path $Repo "packaging\out\gongge-xuban"
  if (Test-Path -LiteralPath $applicationOutput) {
    Write-Host "==> removing stale application output $applicationOutput"
    Remove-Item -LiteralPath $applicationOutput -Recurse -Force
  }
}

function ConvertTo-WindowsVersionInfoVersion {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Version
  )

  $match = [regex]::Match($Version, '^[vV]?([0-9]+(?:\.[0-9]+){0,3})')
  if (-not $match.Success) {
    throw "VERSION must start with a numeric version for Windows VersionInfoVersion. Current value: $Version"
  }

  $parts = @($match.Groups[1].Value.Split('.') | ForEach-Object { [int]$_ })
  foreach ($part in $parts) {
    if ($part -lt 0 -or $part -gt 65535) {
      throw "Windows version component out of range 0..65535 in VERSION: $Version"
    }
  }
  while ($parts.Count -lt 4) {
    $parts += 0
  }
  return ($parts[0..3] -join ".")
}

$null = Assert-WindowsX64Host
$env:WINDOWS_VERSION_INFO_VERSION = ConvertTo-WindowsVersionInfoVersion $env:VERSION
Write-Host "Windows VersionInfoVersion: $env:WINDOWS_VERSION_INFO_VERSION"

# A py.exe launcher can exist without an installed Python runtime. Probe each
# candidate instead of only checking whether the launcher is on PATH.
function Get-PythonCommand {
  $candidates = @(
    [pscustomobject]@{ Command = $env:PYTHON; PrefixArgs = @() },
    [pscustomobject]@{ Command = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"; PrefixArgs = @() },
    [pscustomobject]@{ Command = "python"; PrefixArgs = @() },
    [pscustomobject]@{ Command = "py"; PrefixArgs = @("-3.11") },
    [pscustomobject]@{ Command = "py"; PrefixArgs = @("-3") }
  )

  foreach ($candidate in $candidates) {
    if (-not $candidate.Command) { continue }
    if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) { continue }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $candidate.Command @($candidate.PrefixArgs) -c "import sys; assert sys.version_info >= (3, 11)" 2>$null
    $probeExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($probeExitCode -eq 0) { return $candidate }
  }

  throw "Python 3.11 or newer is required. Install it and rerun this script."
}

$PY = Get-PythonCommand
Write-Host "Using Python: $($PY.Command) $($PY.PrefixArgs -join ' ')"
Assert-PythonX64 $PY

Write-Host "==> [1/7] Build frontend"
npm ci --prefix frontend-enterprise --no-audit --no-fund
Assert-NativeCommandSucceeded "npm ci"
npm --prefix frontend-enterprise run build
Assert-NativeCommandSucceeded "Frontend build"

Write-Host "==> [2/7] Create backend venv and install packaging dependencies"
& $PY.Command @($PY.PrefixArgs) -m venv backend\.venv
Assert-NativeCommandSucceeded "Backend virtual environment creation"
backend\.venv\Scripts\python -m pip install -U pip
Assert-NativeCommandSucceeded "pip upgrade"
# Extract runtime dependencies from pyproject without installing the project itself.
Push-Location backend
$deps = .\.venv\Scripts\python -c "import tomllib,pathlib; print('\n'.join(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']))"
Assert-NativeCommandSucceeded "Runtime dependency extraction"
$deps | Out-File -Encoding utf8 ..\packaging\_win_reqs.txt
Pop-Location
backend\.venv\Scripts\python -m pip install -r packaging\_win_reqs.txt
Assert-NativeCommandSucceeded "Backend dependency installation"
backend\.venv\Scripts\python -m pip install "pyinstaller>=6.6.0" "certifi>=2024.2.2"
Assert-NativeCommandSucceeded "Packaging dependency installation"

Write-Host "==> [3/7] Build PyInstaller application"
Stop-ExistingProductProcesses
Clear-ApplicationBuildOutput
Push-Location backend
.\.venv\Scripts\pyinstaller ..\packaging\gongge-xuban.spec --noconfirm --clean --distpath ..\packaging\out --workpath ..\packaging\build
Assert-NativeCommandSucceeded "PyInstaller build"
Pop-Location
Assert-PeX64 "packaging\out\gongge-xuban\gongge-xuban.exe"

$signingConfigured = Test-SigningConfigured
if ($signingConfigured) {
  Write-Host "Signing gongge-xuban.exe"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging\sign_windows.ps1 `
    -FilePath packaging\out\gongge-xuban\gongge-xuban.exe
  Assert-NativeCommandSucceeded "gongge-xuban.exe signing"
  $env:WINDOWS_SIGN_ENABLED = "1"
} else {
  $env:WINDOWS_SIGN_ENABLED = "0"
  Write-Warning "Code signing is not configured; Windows artifacts will be UNSIGNED."
}

Write-Host "==> [4/7] Bundle the Python skill runtime"
backend\.venv\Scripts\python packaging\fetch_runtime_python.py packaging\runtime_dl --expect-arch x86_64
Assert-NativeCommandSucceeded "Python skill runtime download"
if (Test-Path packaging\out\gongge-xuban\runtime) { Remove-Item -Recurse -Force packaging\out\gongge-xuban\runtime }
Copy-Item -Recurse -Force packaging\runtime_dl\python packaging\out\gongge-xuban\runtime
Assert-BundlePeX64 "packaging\out\gongge-xuban"

Write-Host "==> [5/7] Build the Inno Setup installer"
$isccCandidates = @(
  $env:ISCC,
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ }
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  throw "Inno Setup 6 was not found. Install it or set ISCC to the full path of ISCC.exe."
}
Write-Host "Using Inno Setup: $iscc"
$unsignedInstaller = "packaging\out\Gongge-Xuban-setup.exe"
if (Test-Path $unsignedInstaller) {
  Remove-Item -Force $unsignedInstaller
}
if ($signingConfigured) {
  $signScript = (Resolve-Path packaging\sign_windows.ps1).Path
  $signCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$signScript`" -FilePath `$f"
  & "$iscc" "/Sgongge-xuban=$signCommand" packaging\installer\gongge-xuban.iss
} else {
  & "$iscc" packaging\installer\gongge-xuban.iss
}
Assert-NativeCommandSucceeded "Inno Setup build"
if (-not (Test-Path $unsignedInstaller)) {
  throw "Inno Setup completed without producing $unsignedInstaller."
}

Write-Host "==> [6/7] Name the release artifact"
$out = "packaging\out\Gongge-Xuban-windows-x64-setup.exe"
if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Force }
Move-Item -LiteralPath $unsignedInstaller -Destination $out
if ($signingConfigured) {
  $signature = Get-AuthenticodeSignature $out
  if ($signature.Status -ne "Valid") {
    throw "Final installer signature is not valid: $($signature.StatusMessage)"
  }
  Write-Host "Authenticode signature valid: $($signature.SignerCertificate.Subject)"
}
Write-Host "==> [7/7] Smoke-test installation, frozen runtime, health and uninstall"
$smokeScript = (Resolve-Path packaging\smoke_windows.ps1).Path
$resolvedOut = (Resolve-Path -LiteralPath $out).Path
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $smokeScript `
  -InstallerPath $resolvedOut
Assert-NativeCommandSucceeded "Windows native smoke test"
Write-Host "built $out"
Get-ChildItem packaging\out\Gongge-Xuban-windows-x64-setup.exe
