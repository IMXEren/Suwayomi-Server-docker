#!/bin/sh
# Attach x11vnc to the display service's X server and serve the noVNC client in front of it.
#
# The display runs Xvfb with access control disabled, so no Xauthority is involved. The socket
# directory is a shared volume, which is what lets this container see the X server at all.
set -eu

DISPLAY_NUM="${DISPLAY_NUM:-0}"
WEB_PORT="${VNC_WEB_PORT:-6080}"
SOCKET="/tmp/.X11-unix/X${DISPLAY_NUM}"

# The display service is started first and its healthcheck covers the socket, but a restart can
# still race it, so wait rather than exiting into a restart loop.
for _ in $(seq 1 60); do
    if [ -S "$SOCKET" ]; then
        break
    fi
    sleep 1
done
if [ ! -S "$SOCKET" ]; then
    echo "X socket $SOCKET never appeared" >&2
    exit 1
fi

# Loopback only: websockify runs in this same container, so the raw VNC port never leaves it.
# `-shared` allows more than one client, `-forever` keeps serving after a client disconnects.
# `-noshm` is required, not optional: the X server is in another container, so the MIT-SHM
# shared memory attach fails with BadAccess and x11vnc exits before serving anything.
#
# `-threads` gives each client its own input and output threads, so encoding one client's frame
# does not block servicing another's input.
#
# Compression is not configured here, and cannot be: this build rejects `-zlib`, `-quality` and
# `-compresslevel` as unrecognized options and refuses to start, verified by running it with them.
# The framing is therefore chosen by the client, which is noVNC in this deployment: it asks for
# Tight and sets the quality and compression levels itself (its defaults are quality 6, which
# enables JPEG for photographic areas and leaves text in lossless palette mode, and compression 2).
# Those two are the real compression levers, and both are reachable from the noVNC settings panel
# at the cost of a smaller panel: see docs/DEPLOY.md for the measured effect of each.
# what is not a lever here is damage tracking: X DAMAGE is available on this display and x11vnc
# uses it for polling hints by default, so nothing disables it (the option to look for would be
# -noxdamage, which is deliberately absent).
x11vnc \
    -display ":${DISPLAY_NUM}" \
    -rfbport 5900 \
    -localhost \
    -shared \
    -threads \
    -forever \
    -nopw \
    -noshm \
    -quiet \
    -bg \
    -o /tmp/x11vnc.log

# Foreground process: the container lives exactly as long as the web surface does.
exec websockify --web=/usr/share/novnc "${WEB_PORT}" 127.0.0.1:5900
