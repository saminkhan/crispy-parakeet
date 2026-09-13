#!/usr/bin/env bash
# Make GTA V render at the resolution it claims to.
#
# ★★ GTA V is DPI-unaware. On a display with Windows scaling above 100% it renders
# into a VIRTUALISED surface: settings.xml says 1920x1080, the window measures
# 1920x1080, and the backbuffer is smaller. Measured here: a 2560x1440 panel at
# 150% scaling gave a 1708x960 backbuffer.
#
# ⚠ Nothing reports this. The plugin resamples up to the requested frame size, so
# the stream is the right shape and the frames look fine -- they are upsampled from
# a smaller render, and meta.json's K describes a sampling grid that does not exist.
# It also cost 45 ms per frame in a scalar per-pixel resample and inflated every
# payload to 6.2 MB.
#
# The fix is the per-user compatibility flag the exe's Compatibility tab sets
# (Change high DPI settings -> Override -> Application). Reversible; touches no
# game files.
set -u
G="${1:-D:\\SteamLibrary\\steamapps\\common\\Grand Theft Auto V}"
powershell.exe -NoProfile -Command "
\$k='HKCU:\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers'
if (-not (Test-Path \$k)) { New-Item -Path \$k -Force | Out-Null }
foreach (\$n in @('GTA5.exe','PlayGTAV.exe','GTA5_Enhanced.exe')) {
  \$e = '$G' + '\' + \$n
  if (Test-Path \$e) { Set-ItemProperty -Path \$k -Name \$e -Value '~ HIGHDPIAWARE'; Write-Output ('DPI-aware: ' + \$e) }
}" 2>&1 | grep -v '^$'

echo
echo "Also set in <Documents>/Rockstar Games/GTA V/settings.xml:"
echo "  <Windowed value=\"1\"/>   # NOT borderless: borderless on a DPI-aware app takes"
echo "                           # the whole panel, 1.8x the pixels for no gain"
echo "  <VSync value=\"0\"/>      # Present is what the capture waits on"
echo
echo "Verify after the next launch -- the plugin logs the real backbuffer once:"
echo "  grep backbuffer \"<GTA V dir>/DeepGTAV.log\""
echo "  [longtail] backbuffer 1920x1080 ... (target 1920x1080)   <- correct"
