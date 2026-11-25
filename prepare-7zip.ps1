$ErrorActionPreference = 'Stop'
$work = Join-Path $PSScriptRoot 'build/7zip'
$output = Join-Path $PSScriptRoot '7zip'
$msi = Join-Path $work '7z2605-x64.msi'
$expected = '3fe4ab99fa15629c652944309a2b2bc6e41fc933fc6b63f5df1ebcf3aea68263'
New-Item -ItemType Directory -Path $work, $output -Force | Out-Null
if (!(Test-Path -LiteralPath $msi)) {
    Invoke-WebRequest -Uri 'https://github.com/ip7z/7zip/releases/download/26.05/7z2605-x64.msi' -OutFile $msi
}
if ((Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash -ne $expected) {
    throw 'Official 7-Zip installer SHA-256 mismatch.'
}
$expanded = Join-Path $work 'expanded'
$source = Join-Path $expanded 'Files/7-Zip'
$names = '7z.exe', '7z.dll', 'License.txt'
if ($names | Where-Object { !(Test-Path -LiteralPath (Join-Path $source $_)) }) {
    $process = Start-Process msiexec.exe -ArgumentList "/a ""$msi"" TARGETDIR=""$expanded"" /qn" -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Could not unpack 7-Zip MSI ($($process.ExitCode))." }
}
foreach ($name in $names) {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination $output -Force
}
