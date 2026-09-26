# Window manager for the browser display.
#
# The display service runs a bare Xvfb, and without a window manager a window keeps whatever size
# and position it asked for, even when that is larger than the screen. This service supplies only
# window management: no desktop, no panel, no wallpaper. It is a separate service rather than part
# of the vnc image because it has a different job and a different lifecycle, and it needs nothing
# from that image.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openbox \
        wmctrl \
        x11-utils \
    && rm -rf /var/lib/apt/lists/*

COPY rc.xml /etc/openbox/rc.xml
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
