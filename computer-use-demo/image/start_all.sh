#!/bin/bash

set -e

export DISPLAY=:${DISPLAY_NUM}

# Clean up stale X lock files (needed when running from a committed image)
rm -f /tmp/.X${DISPLAY_NUM}-lock /tmp/.X11-unix/X${DISPLAY_NUM}

./xvfb_startup.sh
./tint2_startup.sh
./mutter_startup.sh
./x11vnc_startup.sh
