<#
.SYNOPSIS
    Signs a program file with a code-signing certificate (Authenticode, SHA-256) and checks the result.

.DESCRIPTION
    A signature tells Windows who published a program and that the file has not been changed since. It needs
    a code-signing certificate issued to you by a certificate authority; this script does not create one.

    Use -Thumbprint for a certificate in the Windows certificate store, which is also how certificates on
    a hardware token appear, or -PfxFile for a certificate file. packaging\build.ps1 calls this script when
    it is given -SignThumbprint.

    The signature is time-stamped by default, so it stays valid after the certificate expires. That sends a
    hash of the signature (nothing else) to the time-stamp server.

.PARAMETER File
    The program to sign.

.PARAMETER Thumbprint
    Thumbprint of a code-signing certificate in Cert:\CurrentUser\My or Cert:\LocalMachine\My.

.PARAMETER PfxFile
    A certificate file with its private key (.pfx / .p12). The password is asked for unless -PfxPassword is given.

.PARAMETER AllowUntrusted
    Accept a signature whose certificate Windows does not trust (a self-signed test certificate). Such a
    signature proves that signing works, but it does not remove the Windows warning for other people.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\sign.ps1 -File dist\RedactNER\RedactNER.exe -Thumbprint 0123456789ABCDEF0123456789ABCDEF01234567
#>
param(
    [Parameter(Mandatory)] [string] $File,
    [string] $Thumbprint,
    [string] $PfxFile,
    [securestring] $PfxPassword,
    [string] $TimestampUrl = "http://timestamp.digicert.com",
    [switch] $AllowUntrusted
)

$ErrorActionPreference = "Stop"
if ([bool] $Thumbprint -eq [bool] $PfxFile) { throw "Give exactly one of -Thumbprint or -PfxFile." }
$target = (Resolve-Path $File).Path

$certificate = $null
$fromFile = $false
if ($Thumbprint) {
    $clean = ($Thumbprint -replace "[^0-9A-Fa-f]", "").ToUpper()
    $certificate = Get-ChildItem "Cert:\CurrentUser\My\$clean", "Cert:\LocalMachine\My\$clean" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $certificate) { throw "No certificate with thumbprint $clean is in the Windows certificate store." }
} else {
    if (-not $PfxPassword) { $PfxPassword = Read-Host "Password of $(Split-Path $PfxFile -Leaf)" -AsSecureString }
    $certificate = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2((Resolve-Path $PfxFile).Path, $PfxPassword)
    $fromFile = $true
}

try {
    if (-not $certificate.HasPrivateKey) { throw "The certificate has no private key, so it cannot sign." }
    $usages = @($certificate.EnhancedKeyUsageList | ForEach-Object { $_.ObjectId })
    if ($usages.Count -and $usages -notcontains "1.3.6.1.5.5.7.3.3") { throw "The certificate is not a code-signing certificate." }

    $options = @{ FilePath = $target; Certificate = $certificate; HashAlgorithm = "SHA256"; IncludeChain = "NotRoot" }
    if ($TimestampUrl) { $options.TimestampServer = $TimestampUrl }
    [void] (Set-AuthenticodeSignature @options)

    $signature = Get-AuthenticodeSignature $target
    $trusted = $signature.Status -eq "Valid"
    $untrustedOnly = $signature.Status -eq "UnknownError" -and $signature.SignerCertificate -and $signature.SignerCertificate.Thumbprint -eq $certificate.Thumbprint
    if (-not $trusted -and -not ($AllowUntrusted -and $untrustedOnly)) {
        throw "The file was not signed correctly: $($signature.Status). $($signature.StatusMessage)"
    }
    Write-Host ("Signed {0} as '{1}' ({2}; time-stamped: {3})." -f (Split-Path $target -Leaf), $certificate.Subject,
        $(if ($trusted) { "trusted by Windows" } else { "NOT trusted by Windows" }), [bool] $signature.TimeStamperCertificate)
} finally {
    if ($fromFile -and $certificate) { $certificate.Reset() }   # forget the key that was read from the file
}
