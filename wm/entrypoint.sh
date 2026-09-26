#!/bin/sh
# Manage the windows of the browser display.
#
# The display service publishes its X socket on a shared volume and runs with access control
# disabled, so no Xauthority is involved. The socket is waited for rather than assumed: this
# service can be restarted on its own, and it must not exit into a restart loop if it wins the
# race against the display.
set -eu

DISPLAY_NUM="${DISPLAY_NUM:-0}"
SOCKET="/tmp/.X11-unix/X${DISPLAY_NUM}"

# The display is named by its number, so the address is derived here rather than configured twice.
DISPLAY=":${DISPLAY_NUM}"
export DISPLAY

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

# The window manager runs in the background and the fit check stays in the foreground, so the
# container lives exactly as long as both do. The log goes to stderr, which is where a container's
# output belongs, so a configuration error is visible in the service logs instead of being written
# to a file nothing reads.
openbox --config-file /etc/openbox/rc.xml 2>&1 &
openbox_pid=$!
trap 'kill "$openbox_pid" 2>/dev/null' TERM INT

# A window is maximized when the manager first takes it over, but a client can resize itself after
# that: the browser restores a placement saved for a different screen, which ends up larger than
# this one and puts the right edge and the bottom of the window out of reach. Re-asserting the
# maximize for a window that is larger than the work area makes the fit independent of which of the
# two happens last, and does nothing at all while the window already fits.
while kill -0 "$openbox_pid" 2>/dev/null; do
    sleep 2
    work_area=$(xprop -root _NET_WORKAREA 2>/dev/null | sed 's/.*= //' | cut -d, -f1-4 | tr -d ' ') || continue
    work_width=$(echo "$work_area" | cut -d, -f3)
    work_height=$(echo "$work_area" | cut -d, -f4)
    case "$work_width$work_height" in
        '' | *[!0-9]*) continue ;;
    esac
    wmctrl -l -G 2>/dev/null | while read -r window _ _ _ width height _; do
        case "$width$height" in
            '' | *[!0-9]*) continue ;;
        esac
        if [ "$width" -gt "$work_width" ] || [ "$height" -gt "$work_height" ]; then
            echo "refitting window $window from ${width}x${height} to the ${work_width}x${work_height} work area" >&2
            wmctrl -i -r "$window" -b add,maximized_vert,maximized_horz 2>/dev/null || true
        fi
    done
done

wait "$openbox_pid"
