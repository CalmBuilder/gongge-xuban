param(
  [Parameter(Position = 0)]
  [string]$Command = ""
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Candidates = @()

if ($env:PYTHON) {
  $Candidates += [pscustomobject]@{ File = $env:PYTHON; Prefix = @() }
}
$Candidates += [pscustomobject]@{ File = "py"; Prefix = @("-3.11") }
$Candidates += [pscustomobject]@{ File = "py"; Prefix = @("-3") }
$Candidates += [pscustomobject]@{ File = "python"; Prefix = @() }

$Python = $null
foreach ($Candidate in $Candidates) {
  try {
    $CandidatePrefix = $Candidate.Prefix
    & $Candidate.File @CandidatePrefix -c "import sys; raise SystemExit(sys.version_info < (3, 11))" 2>$null
    if ($LASTEXITCODE -eq 0) {
      $Python = $Candidate
      break
    }
  }
  catch [System.Management.Automation.CommandNotFoundException] {
    continue
  }
}

if ($null -eq $Python) {
  Write-Error "Python 3.11 or newer is required."
  exit 1
}

if ($args.Count -gt 0) {
  Write-Error "Usage: .\app.ps1 [dev|status|stop]"
  exit 2
}

$LifecycleArgs = switch ($Command) {
  "" { @("up", "--mode", "production", "--detach") }
  "dev" { @("up", "--mode", "development") }
  "status" { @("status") }
  "stop" { @("down") }
  default {
    Write-Error "Usage: .\app.ps1 [dev|status|stop]"
    exit 2
  }
}
$LifecycleArgs = @($LifecycleArgs)

$PythonPrefix = $Python.Prefix
$Lifecycle = Join-Path $RootDir "scripts\app.py"
& $Python.File @PythonPrefix $Lifecycle @LifecycleArgs
exit $LASTEXITCODE
