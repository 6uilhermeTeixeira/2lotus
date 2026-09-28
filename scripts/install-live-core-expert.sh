#!/usr/bin/env bash
set -euo pipefail

prefix="${LIVE_CORE_WINEPREFIX:-$HOME/.local/share/mt5-live-wine}"
mt5_root="$prefix/drive_c/Program Files/MetaTrader 5"
source_file="$HOME/projects/1lotus/mt5/experts/LiveCoreBridgeEA.mq5"
expert_file="$mt5_root/MQL5/Experts/LiveCoreBridgeEA.mq5"
preset_source="$HOME/projects/1lotus/mt5/presets/live-core-safe.set"
preset_file="$mt5_root/MQL5/Presets/live-core-safe.set"
metaeditor="$mt5_root/MetaEditor64.exe"

if [[ ! -f "$metaeditor" ]]; then
    echo "MetaEditor ainda não instalado no prefixo Live Core." >&2
    exit 1
fi

install -m 600 "$source_file" "$expert_file"
install -d -m 700 "$mt5_root/MQL5/Presets"
install -m 600 "$preset_source" "$preset_file"
export DISPLAY=:6
export XAUTHORITY="${XDG_CONFIG_HOME:-$HOME/.config}/live-core-mt5-session/Xauthority"
export WINEPREFIX="$prefix"
export WINEDEBUG=-all
wine "$metaeditor" "/compile:C:\\Program Files\\MetaTrader 5\\MQL5\\Experts\\LiveCoreBridgeEA.mq5" \
    "/log:C:\\Program Files\\MetaTrader 5\\MQL5\\Experts\\LiveCoreBridgeEA-compile.log"

test -f "$mt5_root/MQL5/Experts/LiveCoreBridgeEA.ex5"
echo "LiveCoreBridgeEA e live-core-safe.set instalados. EnableOrderExecution permanece false."
