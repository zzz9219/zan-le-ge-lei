$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$taskExpectedDb = Join-Path $PSScriptRoot 'runtime\library.sqlite3'
$taskExisting = $null
try {
    $taskExisting = Invoke-RestMethod -Uri 'http://127.0.0.1:19423/api/status' -TimeoutSec 5
} catch {}
if ($taskExisting) {
    if ($taskExisting.db -ne $taskExpectedDb) { throw 'Another library uses port 19423. Stop that web service before opening this copy.' }
    Start-Process 'http://127.0.0.1:19423/'
    exit
}
Start-Process -FilePath $taskPython -ArgumentList 'web_runner.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
for ($taskTry = 0; $taskTry -lt 30; $taskTry++) {
    try {
        $taskResponse = Invoke-WebRequest -Uri 'http://127.0.0.1:19423/api/status' -UseBasicParsing -TimeoutSec 2
        if ($taskResponse.StatusCode -eq 200) { Start-Process 'http://127.0.0.1:19423/'; exit }
    } catch {}
    Start-Sleep -Milliseconds 300
}
throw 'Web startup failed; inspect runtime\web-service.log.'
