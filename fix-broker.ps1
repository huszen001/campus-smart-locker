# campus-locker: make Mosquitto listen on the LAN and require username+password.
# Requires Administrator.
#
# Why: Mosquitto 2.x ships bound to 127.0.0.1 only with allow_anonymous false, so the
#      ESP32 cannot connect. This does NOT open up authentication -- it switches to a
#      config where a username+password is mandatory.
#
# GOTCHA that cost us a long debugging session:
#   mosquitto_passwd creates the password file readable ONLY by the user who ran it.
#   The Windows service runs as LocalSystem, so it cannot read that file and the broker
#   dies instantly with a clean exit code, no listener, and this line only in the log:
#       password-file: Error: Unable to open pwfile "...\passwd".
#   The log itself is written with a SYSTEM-only ACL, so it looks like nothing was logged.
#   Fix: grant SYSTEM read access on the password file (step 2 below).
#
# NOTE: this file is deliberately ASCII-only. Windows PowerShell 5.1 reads .ps1 as
#       ANSI/GBK when the file has no UTF-8 BOM, which corrupts non-ASCII text and can
#       break parsing entirely. Keep it ASCII.
$ErrorActionPreference = "Stop"

# The elevated window closes the moment it finishes, so log everything to a file.
Start-Transcript -Path "D:\ClaudeWorkspace\campus-locker\fix-broker.log" -Force | Out-Null

$mosqDir = "C:\Program Files\Mosquitto"
$conf    = Join-Path $mosqDir "mosquitto.conf"
$passwd  = Join-Path $mosqDir "passwd"
$user    = "locker"
$pass    = "locker123"   # change this if you like; then update app.py / device_sim.py / firmware

Write-Host "[1/4] create account: $user"
& (Join-Path $mosqDir "mosquitto_passwd.exe") -c -b $passwd $user $pass

Write-Host "[2/4] let the service account (LocalSystem, S-1-5-18) read the password file"
# Without this the service starts and immediately exits -- see GOTCHA above.
& icacls.exe $passwd /grant "*S-1-5-18:(R)" | Out-Null
& icacls.exe $passwd

Write-Host "[3/4] write config"
if ((Get-Content $conf -Raw) -match 'campus-locker') {
    Write-Host "      already configured, skipping"
} else {
    Add-Content -Path $conf -Encoding Ascii -Value @(
        "",
        "# --- campus-locker: LAN access with username/password auth ---",
        "listener 1883 0.0.0.0",
        "allow_anonymous false",
        "password_file $passwd"
    )
    Write-Host "      config appended"
}

Write-Host "[4/4] restart service"
Restart-Service mosquitto
Start-Sleep -Seconds 3

Write-Host ""
Write-Host "--- port 1883 listeners (expect 0.0.0.0) ---"
$lis = Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue
if ($lis) {
    $lis | Select-Object LocalAddress, LocalPort | Format-Table
} else {
    Write-Host "NOTHING IS LISTENING - check the log note at the top of this file"
}
Write-Host "service status: $((Get-Service mosquitto).Status)"

Stop-Transcript | Out-Null
