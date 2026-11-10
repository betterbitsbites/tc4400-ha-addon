ARG BUILD_FROM
FROM ${BUILD_FROM}

LABEL org.opencontainers.image.title="Technicolor TC4400 Modem Scraper" \
      org.opencontainers.image.authors="Philipp Reichmuth (phrxmd), mmiller7, coaxx77" \
      org.opencontainers.image.licenses="CC-BY-NC-SA-4.0" \
      org.opencontainers.image.source="https://github.com/phrxmd/Technicolor_modem_scrape"

# GNU sed/awk for the HTML parsing, mosquitto clients for MQTT,
# python3 (standard library only) for the dashboard generator
RUN apk add --no-cache bash curl gawk sed coreutils mosquitto-clients python3

COPY rootfs /
RUN chmod a+x /run.sh /usr/bin/technicolor_signal_dump.sh /usr/bin/tc4400_dashboard.py

CMD [ "/run.sh" ]
