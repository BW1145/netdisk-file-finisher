$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Push-Location $PSScriptRoot
try {
    python -m venv .venv-build
    if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment.' }
    & ./.venv-build/Scripts/python.exe -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install build requirements.' }
    ./prepare-7zip.ps1
    & ./.venv-build/Scripts/python.exe -m PyInstaller --onefile --windowed --noupx --name NetdiskFinisher --add-data '7zip/7z.exe:7zip' --add-data '7zip/7z.dll:7zip' --add-data '7zip/License.txt:7zip' app.py
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
    Write-Output 'Ready: dist/NetdiskFinisher.exe'
}
finally {
    Pop-Location
}
