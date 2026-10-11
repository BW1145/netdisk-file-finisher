$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Push-Location $PSScriptRoot
try {
    python -m venv .venv-build
    if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment.' }
    & ./.venv-build/Scripts/python.exe -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install build requirements.' }
    & ./.venv-build/Scripts/python.exe -m PyInstaller --onefile --windowed --noupx --name NetdiskFinisher app.py
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
    Write-Output 'Ready: dist/NetdiskFinisher.exe'
}
finally {
    Pop-Location
}
