param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$Commit
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $actualCommit = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $Commit) { throw 'Build commit does not match checkout.' }
    $package = "dist/NetdiskFinisher-$Version-Windows-x64"
    New-Item -ItemType Directory -Path $package -Force | Out-Null
    Copy-Item -LiteralPath dist/NetdiskFinisher.exe, README.md, LICENSE -Destination $package
    $info = [ordered]@{
        version = $Version
        commit = $Commit
        exe_sha256 = (Get-FileHash dist/NetdiskFinisher.exe -Algorithm SHA256).Hash.ToLowerInvariant()
    } | ConvertTo-Json
    $info | Set-Content -LiteralPath "$package/BUILD_INFO.json" -Encoding utf8
    New-Item -ItemType Directory -Path dist/release -Force | Out-Null
    Copy-Item -LiteralPath dist/NetdiskFinisher.exe, "$package/BUILD_INFO.json" -Destination dist/release
    $zipName = "NetdiskFinisher-$Version-Windows-x64.zip"
    Compress-Archive -Path "$package/*" -DestinationPath "dist/release/$zipName" -Force
    $checksums = foreach ($name in @('NetdiskFinisher.exe', $zipName, 'BUILD_INFO.json')) {
        $hash = (Get-FileHash -LiteralPath "dist/release/$name" -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $name"
    }
    $checksums | Set-Content -LiteralPath dist/release/SHA256SUMS.txt -Encoding ascii
}
finally {
    Pop-Location
}
