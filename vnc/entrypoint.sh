#!/bin/sh
# Attach x11vnc to the display service's X server and serve the noVNC client in front of it.
#
# The display runs Xvfb with access control disabled, so no Xauthority is involved. The socket
# directory is a shared volume, which is what lets this container see the X server at all.
#
# x11vnc has been observed to stop serving while staying alive: it holds the listening socket,
# accepts nothing, and the accept queue fills until clients time out mid-handshake, which shows up
# as noVNC sitting on "Connecting...". It never recovers by itself, and only the TCP port being
# open makes it look healthy. So it is supervised here rather than started once and trusted, and
# "serving" is defined as completing the RFB banner exchange, not as holding the port.
set -eu

DISPLAY_NUM="${DISPLAY_NUM:-0}"
WEB_PORT="${VNC_WEB_PORT:-6080}"
WATCH_INTERVAL="${VNC_WATCH_INTERVAL_SECONDS:-20}"
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
start_x11vnc() {
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
        -o /tmp/x11vnc.log &
    x11vnc_pid=$!
    sleep 2
}

# The banner is the first thing any client needs, and the whole point is that it can go missing
# while the port stays open, so the check reads it rather than connecting and hanging up.
rfb_serving() {
    timeout 8 bash -c 'exec 3<>/dev/tcp/127.0.0.1/5900; head -c 12 <&3; exec 3<&-' 2>/dev/null | grep -q 'RFB 003'
}

start_x11vnc
websockify --web=/usr/share/novnc "${WEB_PORT}" 127.0.0.1:5900 &
websockify_pid=$!

# The container lives as long as its two servers do; either one dying is a restart-worthy failure.
shutdown() {
    kill "$x11vnc_pid" "$websockify_pid" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

wedges=0
while :; do
    sleep "$WATCH_INTERVAL"

    if ! kill -0 "$websockify_pid" 2>/dev/null; then
        echo "websockify exited; restarting the container" >&2
        exit 1
    fi

    if rfb_serving; then
        wedges=0
        continue
    fi

    wedges=$((wedges + 1))
    echo "x11vnc is not serving the RFB handshake (wedges=${wedges}); restarting it" >&2
    kill "$x11vnc_pid" 2>/dev/null || true
    wait "$x11vnc_pid" 2>/dev/null || true
    if [ "$wedges" -ge 3 ]; then
        # Restarting it is not helping, so let Docker replace the whole container instead of
        # looping here with a dead display.
        echo "x11vnc kept wedging after restarts; restarting the container" >&2
        exit 1
    fi
    start_x11vnc
done
