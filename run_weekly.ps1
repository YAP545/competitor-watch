# Weekly Competitor Watch execution script for Windows Task Scheduler / PowerShell
[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$ScriptDir = If ($PSScriptRoot) { $PSScriptRoot } Else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
Set-Location -Path $ScriptDir

$LogFile = Join-Path $ScriptDir "run.log"
$Timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
"[$Timestamp] Starting weekly Competitor Watch run..." | Out-File -FilePath $LogFile -Append -Encoding utf8

# Load .env if present (optional local configuration)
$EnvFile = Join-Path $ScriptDir ".env"
If (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        If ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $name, $value = $line.Split("=", 2)
            $name = $name.Trim()
            $value = $value.Trim().Trim('"').Trim("'")
            If ($name -and -not [System.Environment]::GetEnvironmentVariable($name)) {
                [System.Environment]::SetEnvironmentVariable($name, $value)
            }
        }
    }
}

# Resolve Python executable
$PythonCmd = (Get-Command python -ErrorAction SilentlyContinue)
If (-not $PythonCmd) {
    $PythonCmd = (Get-Command py -ErrorAction SilentlyContinue)
}

If (-not $PythonCmd) {
    $ErrMsg = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] ERROR: Python executable not found in PATH."
    $ErrMsg | Out-File -FilePath $LogFile -Append -Encoding utf8
    Write-Error $ErrMsg
    Exit 1
}

# Execute competitor_watch.py
$AppScript = Join-Path $ScriptDir "competitor_watch.py"
& $PythonCmd.Source $AppScript 2>&1 | Tee-Object -FilePath $LogFile -Append
$ExitCode = $LASTEXITCODE

$EndTimestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
"[$EndTimestamp] Competitor Watch run completed with exit code: $ExitCode`n" | Out-File -FilePath $LogFile -Append -Encoding utf8

Exit $ExitCode
