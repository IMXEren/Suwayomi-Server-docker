# VNC surface for the browser display.
#
# x11vnc attaches to the Xvfb socket the display service publishes, and websockify serves the
# noVNC client with a WebSocket bridge in front of it. Built here rather than pulled as a third
# party all-in-one image, because those start their own X server and cannot attach to the one the
# browser already renders to.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        novnc \
        vncsnapshot \
        websockify \
        x11-utils \
        x11vnc \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# The noVNC client and its WebSocket endpoint. Published nowhere; the app brokers access.
# x11-utils is there for xwininfo, which lists the windows on the display and so proves the
# browser is on the surface a frame was taken from.
EXPOSE 6080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
