# Convenience launcher: prefers a project venv, then the available Codex Python,
# then a supported Python on PATH. No packages are downloaded automatically.
$ErrorActionPreference = 'Stop'
$dapCandidates = @(
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
$dapPython = $null
foreach ($dapCandidate in $dapCandidates) {
    if (Test-Path -LiteralPath $dapCandidate) {
        & $dapCandidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $dapPython = $dapCandidate; break }
    }
}
if (-not $dapPython) {
    $dapCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($dapCommand) {
        & $dapCommand.Source -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $dapPython = $dapCommand.Source }
    }
}
if (-not $dapPython) { throw 'Python 3.9+ is required. Create .venv or add a supported Python to PATH.' }
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($args.Count -gt 0 -and $args[0] -eq 'test') {
        & $dapPython -m unittest discover -v
    } else {
        & $dapPython -m dapremote @args
    }
    $dapExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $dapExitCode
