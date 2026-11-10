[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$Wheelhouse = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Checked {
    param([string]$Executable, [string[]]$ArgumentList)
    & $Executable @ArgumentList
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $Executable (exit $LASTEXITCODE)" }
}

if ($env:OS -ne "Windows_NT") { throw "A real Windows x64 build machine is required." }
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -LiteralPath $Root
if ($Python -eq "") {
    $Runtime = & py -3.13 -c "import sys; print(sys.executable)"
} else {
    $Runtime = & $Python -c "import sys; print(sys.executable)"
}
if ($LASTEXITCODE -ne 0) { throw "Install CPython 3.13 x64 on the build machine (recommended 3.13.12)." }
$Runtime = ($Runtime | Select-Object -Last 1).Trim()
Invoke-Checked $Runtime @("-c", "import sys,struct,platform; assert sys.version_info[:2]==(3,13) and struct.calcsize('P')==8 and platform.machine().lower() in ('amd64','x86_64'), 'CPython 3.13 x64 required'")

$ProductSlug = & $Runtime -c "from app.branding import PRODUCT_SLUG; print(PRODUCT_SLUG)"
if ($LASTEXITCODE -ne 0) { throw "Cannot read product build identifier." }
$ProductSlug = ($ProductSlug | Select-Object -Last 1).Trim()
$Release = Join-Path $Root ("dist\" + $ProductSlug + "-Windows-x64.zip")
if (Test-Path -LiteralPath $Release) { throw "Release already exists. Move it aside; this script never overwrites artifacts." }
$Work = Join-Path $Root ("build\windows-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Work -Force | Out-Null
$EnvDir = Join-Path $Work "venv"
Invoke-Checked $Runtime @("-m", "venv", $EnvDir)
$BuildPython = Join-Path $EnvDir "Scripts\python.exe"
$InstallArgs = @("-m", "pip", "install", "--only-binary=:all:", "-r", (Join-Path $PSScriptRoot "requirements-build.txt"))
if ($Wheelhouse -ne "") { $InstallArgs += @("--no-index", "--find-links", (Resolve-Path -LiteralPath $Wheelhouse).Path) }
Invoke-Checked $BuildPython $InstallArgs

# The plugin reports the Bash/macOS-only tests as explicit skips on Windows.
Invoke-Checked $BuildPython @("-m", "pytest", "-q", "-p", "tools.windows.pytest_plugin", "--junitxml", (Join-Path $Work "regression.xml"))
$Manifest = Join-Path $Work "bundle-manifest.json"
Invoke-Checked $BuildPython @("-m", "tools.windows.bundle", "manifest", "--output", $Manifest)
$env:CP_WINDOWS_RESOURCE_MANIFEST = $Manifest
try {
    Invoke-Checked $BuildPython @("-m", "PyInstaller", "--clean", "--noconfirm", "--distpath", (Join-Path $Work "dist"), "--workpath", (Join-Path $Work "pyinstaller"), (Join-Path $PSScriptRoot "ConstructionProgress.spec"))
} finally {
    Remove-Item Env:\CP_WINDOWS_RESOURCE_MANIFEST -ErrorAction SilentlyContinue
}
$Portable = Join-Path $Work ("dist\" + $ProductSlug)
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "PORTABLE_README.txt") -Destination (Join-Path $Portable "README.txt")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "VERSION.txt") -Destination $Portable
Invoke-Checked $BuildPython @("-m", "tools.windows.smoke", "--folder", $Portable, "--report", (Join-Path $Portable "BUILD_INFO.json"))
Invoke-Checked $BuildPython @("-m", "tools.windows.bundle", "zip", "--folder", $Portable, "--output", $Release)
Get-FileHash -LiteralPath $Release -Algorithm SHA256 | Format-List
Write-Output "Portable folder: $Portable"
Write-Output "Release ZIP: $Release"
Write-Output "Manual Windows/Edge/Chrome acceptance is still required."
