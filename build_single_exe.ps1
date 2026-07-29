param(
    [string]$Python = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Push-Location $ProjectRoot
try {
    $PythonExe = $Python
    if ([string]::IsNullOrWhiteSpace($PythonExe)) {
        $Candidates = @()
        try {
            $Candidates += (& py -0p 2>$null | ForEach-Object {
                if ($_ -match '([A-Za-z]:\\.*python\.exe)$') { $Matches[1] }
            })
        }
        catch {}
        try {
            $Candidates += (Get-Command python -ErrorAction Stop).Source
        }
        catch {}

        foreach ($Candidate in ($Candidates | Select-Object -Unique)) {
            & $Candidate -c "import PyInstaller, PIL, tkinter" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $PythonExe = $Candidate
                break
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($PythonExe)) {
        throw "No Python environment with PyInstaller, Pillow, and Tkinter was found."
    }

    & $PythonExe -c "import PyInstaller, sys; print(sys.executable); print('PyInstaller', PyInstaller.__version__)"

    & $PythonExe .\tools\generate_lucide_assets.py
    if ($LASTEXITCODE -ne 0) {
        throw "Icon generation failed."
    }

    if (-not $SkipTests) {
        & $PythonExe -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) {
            throw "Tests failed; EXE was not built."
        }
    }

    & $PythonExe -m PyInstaller --noconfirm --clean --distpath .\dist --workpath .\build .\packaging\CCSwitchBatchSender.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }

    $Exe = Get-Item .\dist\CCSwitchBatchSender.exe
    $SizeMb = [Math]::Round($Exe.Length / 1MB, 2)
    Write-Host "Built $($Exe.FullName) ($SizeMb MB)"
}
finally {
    Pop-Location
}
