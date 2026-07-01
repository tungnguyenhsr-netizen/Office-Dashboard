$hermesExe = "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
$dispatcherPy = "C:\Users\YOURNAME\Documents\YourVault\Efforts\Office-Dashboard\scripts\dispatcher.py"
$logPath = "$env:LOCALAPPDATA\hermes\logs\supervisor.log"
$poll = 30

"[$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))] Supervisor started" | Out-File -FilePath $logPath -Encoding utf8 -Append

while ($true) {
    $proc = Get-Process -Name python* -ErrorAction SilentlyContinue | Where-Object { 
        $cmd = (Get-CimInstance -Class Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
        $cmd -like "*dispatcher*"
    }
    if (-not $proc) {
        $msg = "[$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))] Dispatcher not running, starting..."
        "$msg" | Out-File -FilePath $logPath -Encoding utf8 -Append
        Start-Process -FilePath $hermesExe -ArgumentList $dispatcherPy -WindowStyle Hidden
    }
    Start-Sleep -Seconds $poll
}
