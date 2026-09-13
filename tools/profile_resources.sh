#!/usr/bin/env bash
# Sample machine + process resources during capture, to a CSV.
#
# Aimed at the cumulative-failure signature we have: the game dies silently after
# ~1000 capture cycles with no exception. The usual causes leave a trail here:
#   HandleCount / GDI / USER   -> handle leak (Windows kills at 10k GDI per proc)
#   PrivateBytes / WorkingSet  -> memory growth
#   GPU used / util            -> VRAM exhaustion or driver pressure
#   Threads                    -> thread leak
#   [+] the D3D staging texture lives in GPU memory, and grab() runs per capture.
#   $1 = interval seconds (default 5)   $2 = csv path
set -u
INT="${1:-5}"
CSV="${2:-/mnt/d/gtav_longtail/resources.csv}"
PS="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
# ⚠ Singleton. Each sample spawns a PowerShell process, so several concurrent
# instances are a real drain -- four were leaked in one session by restarting this
# without stopping the previous one, which is what pushed the box into memory
# pressure and got a capture run killed.
_me=$$
for _pid in $(pgrep -x bash 2>/dev/null); do
  [ "$_pid" = "$_me" ] && continue
  [ "$_pid" = "$PPID" ] && continue
  case "$(tr '\0' ' ' < /proc/$_pid/cmdline 2>/dev/null)" in
    *profile_resources.sh*) kill -TERM "$_pid" 2>/dev/null ;;
  esac
done

mkdir -p "$(dirname "$CSV")"
echo "t,gta_ws_mb,gta_priv_mb,gta_handles,gta_gdi,gta_user,gta_threads,gpu_used_mb,gpu_util,sys_avail_mb,py_rss_mb" > "$CSV"

while true; do
  read -r ws priv handles gdi user threads <<<"$("$PS" -NoProfile -WindowStyle Hidden -Command '
Add-Type @"
using System;using System.Runtime.InteropServices;
public class G{[DllImport("user32.dll")] public static extern uint GetGuiResources(IntPtr h,uint f);}
"@ -ErrorAction SilentlyContinue
$p = Get-Process GTA5 -ErrorAction SilentlyContinue
if (-not $p) { "0 0 0 0 0 0" } else {
  $gdi  = [G]::GetGuiResources($p.Handle, 0)
  $usr  = [G]::GetGuiResources($p.Handle, 1)
  "{0} {1} {2} {3} {4} {5}" -f [math]::Round($p.WorkingSet64/1MB),
                               [math]::Round($p.PrivateMemorySize64/1MB),
                               $p.HandleCount, $gdi, $usr, $p.Threads.Count
}' 2>/dev/null | tr -d '\r')"
  gpu="$(nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
  gused="${gpu%%,*}"; gutil="${gpu##*,}"
  avail="$("$PS" -NoProfile -WindowStyle Hidden -Command '[math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1KB)' 2>/dev/null | tr -d '\r')"
  pyrss="$(ps -o rss= -C python3 2>/dev/null | awk '{s+=$1} END{print int(s/1024)}')"
  echo "$(date +%H:%M:%S),${ws:-0},${priv:-0},${handles:-0},${gdi:-0},${user:-0},${threads:-0},${gused:-0},${gutil:-0},${avail:-0},${pyrss:-0}" >> "$CSV"
  sleep "$INT"
done
