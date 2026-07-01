$venv = "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
$script = "C:\Users\YOURNAME\Documents\YourVault\Efforts\Office-Dashboard\server.py"
$proc = Get-Process -Name python* -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*8093*' }
if (-not $proc) {
    Start-Process -FilePath $venv -ArgumentList $script -WindowStyle Hidden
}
