#!/bin/bash
# Slopetanium - GitHub release updater for PortMaster ports.
# Structure follows the PortMaster "Basic Launchscript" template.

XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}

if [ -d "/opt/system/Tools/PortMaster/" ]; then
  controlfolder="/opt/system/Tools/PortMaster"
elif [ -d "/opt/tools/PortMaster/" ]; then
  controlfolder="/opt/tools/PortMaster"
elif [ -d "$XDG_DATA_HOME/PortMaster/" ]; then
  controlfolder="$XDG_DATA_HOME/PortMaster"
else
  controlfolder="/roms/ports/PortMaster"
fi

source $controlfolder/control.txt
[ -f "${controlfolder}/mod_${CFW_NAME}.txt" ] && source "${controlfolder}/mod_${CFW_NAME}.txt"
get_controls

GAMEDIR="/$directory/ports/slopetanium"
mkdir -p "$GAMEDIR/conf"
cd "$GAMEDIR"

> "$GAMEDIR/log.txt" && exec > >(tee "$GAMEDIR/log.txt") 2>&1

# Same environment PortMaster's own GUI gets on this CFW (PYSDL2_DLL_PATH,
# PM_PYTHON, frontend refresh), from its shared launcher in env-only mode.
PM_CFW_HOOKS=""
for cfw in muos trimui miyoo spruce retrodeck knulli batocera; do
  if [ "${CFW_NAME,,}" = "$cfw" ] && [ -f "$controlfolder/$cfw/launch_hooks.txt" ]; then
    PM_CFW_HOOKS="$cfw"
  fi
done
pm_hook_frontend_refresh() { :; }
if [ -f "$controlfolder/utils/pm_launch.txt" ]; then
  PM_LAUNCH_ENV_ONLY=Y source "$controlfolder/utils/pm_launch.txt" $PM_CFW_HOOKS
  cd "$GAMEDIR"
else
  export PYSDL2_DLL_PATH="/usr/lib"
fi

export SDL_GAMECONTROLLERCONFIG="$sdl_controllerconfig"
PYTHON="${PM_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python3

# Two-screen handhelds: details on the top panel, the tile row on the bottom one.
# BananaOS (RG DS) turns the bottom panel's backlight off for ports; switch it
# back on (BananaOS's run-port switches it on again after the port too).
if [ -f /run/banana/panel-off ]; then
  SLOP_BL=$(cat /run/banana/panel-off)
  if [ -w "$SLOP_BL/bl_power" ]; then
    echo 0 > "$SLOP_BL/bl_power" && rm -f /run/banana/panel-off
  fi
fi

# Developer switches (SLOP_SCREENS=1|2, SLOP_TOUCH_LOG=1, ...), not shipped.
[ -f "$GAMEDIR/conf/dev.env" ] && set -a && source "$GAMEDIR/conf/dev.env" && set +a

UI="$GAMEDIR/slopetanium-ui.${DEVICE_ARCH}"
pm_platform_helper "$UI"

# Quit: B, Select+Start or the hotkey, handled in the app itself so a quit
# can never cut PortMaster off in the middle of installing a port.
#
# The core (Python: GitHub, PortMaster installs, the web interface) serves the
# native menu on 127.0.0.1 with a token made for this run; the menu
# (slopetanium-ui, C++) draws both screens. Without a menu binary for this
# device, or when it can't start, the core's own Python menu runs instead.
export SLOP_LOCAL_TOKEN="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
rm -f "$GAMEDIR/conf/core.port"
$ESUDO "$PYTHON" "$GAMEDIR/app/slopetanium.py" --serve --controlfolder "$controlfolder" --root "$GAMEDIR" &
CORE=$!
ui_rc=127
if [ -x "$UI" ]; then
  $ESUDO "$UI" --assets "$GAMEDIR/assets" --conf "$GAMEDIR/conf"
  ui_rc=$?
fi
# the core finishes a running install before it stops
kill -TERM $CORE 2>/dev/null
wait $CORE 2>/dev/null
if [ "$ui_rc" != 0 ]; then
  echo "[slopetanium] native menu unavailable ($ui_rc): the Python menu"
  $ESUDO "$PYTHON" "$GAMEDIR/app/slopetanium.py" --controlfolder "$controlfolder" --root "$GAMEDIR"
fi

# Installed or removed ports: let the frontend notice (PortMaster's markers).
pm_hook_frontend_refresh

pm_finish
