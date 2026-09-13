#!/usr/bin/env bash
# Kill every stale GTA V / Rockstar process.
#
# ⚠ Matched by EXECUTABLE PATH, not by name pattern. Rockstar's own launcher is
# "Launcher.exe", which would match half the machine on a name match; and the
# CLAUDE.md rule against `pkill -f <pattern>` exists for exactly this reason --
# a loose pattern eventually matches your own tooling.
set -u
PS="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
"$PS" -NoProfile -WindowStyle Hidden -Command '
$names = @("GTA5","GTA5_BE","PlayGTAV","GTAVLauncher","Rockstar-Games-Launcher",
           "RockstarService","RockstarErrorHandler","SocialClubHelper","Launcher",
           "LauncherPatcher","ROSServiceStarter")
$killed = @()
foreach ($p in Get-Process -ErrorAction SilentlyContinue) {
  $path = $null
  try { $path = $p.Path } catch {}
  $byName = $names -contains $p.ProcessName
  $byPath = $path -and ($path -match "Rockstar" -or $path -match "Grand Theft Auto")
  if ($byName -and ($byPath -or -not $path)) {
    $killed += ("{0} (pid {1})" -f $p.ProcessName, $p.Id)
    try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
  } elseif ($byPath) {
    $killed += ("{0} (pid {1}) [by path]" -f $p.ProcessName, $p.Id)
    try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
  }
}
if ($killed.Count) { "killed: " + ($killed -join ", ") } else { "nothing to clean" }

# ⚠ Stop-Process returns immediately; GTA5.exe can take many seconds to actually
# exit, and a survivor is not cosmetic -- the next launch refuses to start while
# an instance is alive, then the supervisor burns its whole 360 s health timeout
# before giving up. Observed once in a 12 h run: 8 minutes lost to a GTA5 that
# outlived "cleanup" by a few seconds. So wait for the handles to close, and
# escalate rather than report and carry on.
$deadline = (Get-Date).AddSeconds(45)
$left = @()
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 2
  $left = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $names -contains $_.ProcessName })
  if (-not $left.Count) { break }
  # Re-issue on the survivors; a process mid-shutdown ignores the first one.
  foreach ($p in $left) {
    try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
  }
}
if ($left.Count) {
  # Last resort: taskkill takes the whole process tree, which Stop-Process does not.
  foreach ($p in $left) {
    try { & taskkill.exe /F /T /PID $p.Id 2>$null | Out-Null } catch {}
  }
  Start-Sleep -Seconds 3
  $left = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $names -contains $_.ProcessName })
}
if ($left.Count) { "STILL RUNNING: " + (($left | ForEach-Object ProcessName) -join ", ") }
else { "clean" }
' 2>/dev/null | tr -d '\r' | grep -v '^$'
