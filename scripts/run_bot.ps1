param([switch]$Check)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeExe = Join-Path $projectRoot '.venv314\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $runtimeExe -PathType Leaf)) {
    throw 'Create .venv314 with standard 64-bit CPython 3.14.8, then install requirements with constraints.txt. See README.md.'
}

& $runtimeExe -c "import sys, struct, sysconfig; assert sys.implementation.name == 'cpython' and sys.version_info[:3] == (3, 14, 8), 'Use CPython 3.14.8'; assert struct.calcsize('P') == 8 and not sysconfig.get_config_var('Py_GIL_DISABLED'), 'Use standard 64-bit CPython'; print('Runtime: CPython ' + sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) {
    throw 'The default runtime does not match the validated Python 3.14.8 environment.'
}
if ($Check) {
    return
}

Push-Location -LiteralPath $projectRoot
try {
    & $runtimeExe (Join-Path $projectRoot 'bot.py')
    $botExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $botExitCode
