<#
.SYNOPSIS
    Builds the RedactNER desktop application for Windows.

.DESCRIPTION
    Creates an isolated, CPU-only build environment in .venv-build (the first time), packages the application
    with PyInstaller into dist\RedactNER, adds the licence files, and runs the application's own self-test on
    the result. Every package of the build environment is pinned to an exact version and file checksum in
    packaging\requirements.txt, so a download that was altered is refused.
    Run it from any folder:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-Zip] [-SignThumbprint <thumbprint>]

.PARAMETER Zip
    Also write dist\RedactNER-<version>-windows-x64.zip, ready to share, with its SHA-256 checksum beside it.

.PARAMETER SignThumbprint
    Sign RedactNER.exe with the code-signing certificate that has this thumbprint in the Windows certificate
    store (see packaging\sign.ps1). Without it the program is left unsigned, and Windows may warn on first start.
#>
param([switch] $Zip, [string] $SignThumbprint)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv-build\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating the build environment in .venv-build ..."
    uv venv .venv-build --python 3.12 --python-preference only-managed
    if ($LASTEXITCODE -ne 0) { throw "The build environment could not be created." }
    uv pip install --python $python --no-deps --require-hashes --index-strategy unsafe-best-match -r packaging\requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "The packages of the build environment could not be installed." }
    uv pip install --python $python --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw "The project could not be installed into the build environment." }
}

Write-Host "Packaging with PyInstaller (this takes several minutes) ..."
& $python -m PyInstaller packaging\redactner.spec --noconfirm --distpath dist --workpath build --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$app = Join-Path $root "dist\RedactNER"
$exe = Join-Path $app "RedactNER.exe"
Copy-Item (Join-Path $root "LICENSE") (Join-Path $app "LICENSE.txt")
& $python packaging\notices.py (Join-Path $app "THIRD-PARTY-NOTICES.txt")
if ($LASTEXITCODE -ne 0) { throw "The third-party notices could not be written." }

if ($SignThumbprint) {   # before the self-test, so the program that is tested is the one that is shipped
    & (Join-Path $PSScriptRoot "sign.ps1") -File $exe -Thumbprint $SignThumbprint
}

$report = Join-Path $root "build\self-test.json"
Write-Host "Running the self-test of the packaged application ..."
$test = Start-Process -FilePath $exe -ArgumentList "--self-test", "--self-test-output", "`"$report`"" -Wait -PassThru
if ($test.ExitCode -ne 0) { throw "The packaged application failed its self-test (see $report and %LOCALAPPDATA%\RedactNER\logs)." }

$size = (Get-ChildItem $app -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Built {0} ({1:N0} MB). Self-test passed." -f $exe, $size)

if ($Zip) {
    $version = & $python -c "import redactner; print(redactner.__version__)"
    $name = "RedactNER-$version-windows-x64.zip"
    $archive = Join-Path $root "dist\$name"
    Remove-Item $archive, "$archive.sha256" -ErrorAction SilentlyContinue
    tar -a -c -f $archive -C (Join-Path $root "dist") RedactNER
    if ($LASTEXITCODE -ne 0) { throw "The zip archive could not be written." }
    $hash = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLower()
    # the usual "checksum  file name" layout, so sha256sum -c and similar tools can check the download
    [System.IO.File]::WriteAllText("$archive.sha256", "$hash  $name`n", (New-Object System.Text.UTF8Encoding $false))
    Write-Host ("Wrote {0} ({1:N0} MB)." -f $archive, ((Get-Item $archive).Length / 1MB))
    Write-Host "SHA-256: $hash"
}
