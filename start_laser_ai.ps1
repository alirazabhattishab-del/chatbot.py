$workspacePython = "C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (Test-Path $workspacePython) {
    & $workspacePython "$PSScriptRoot\backend_supervisor.py"
    exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    python "$PSScriptRoot\backend_supervisor.py"
    exit $LASTEXITCODE
}

throw "Python was not found. Install Python or use the bundled runtime path."
