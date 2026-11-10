#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
# Original scraper by Philipp Reichmuth (phrxmd):
#   https://github.com/phrxmd/Technicolor_modem_scrape
# based on scripts by mmiller7. License: CC-BY-NC-SA 4.0
# Home Assistant add-on entrypoint: read options, find MQTT, scrape in a loop.

export MODEM_URL="$(bashio::config 'modem_url')"
export MODEM_USERNAME="$(bashio::config 'modem_username')"
export MODEM_PASSWORD="$(bashio::config 'modem_password')"
export MQTT_TOPIC_PREFIX="$(bashio::config 'mqtt_topic_prefix')"
export MQTT_UID_PREFIX="$(bashio::config 'mqtt_uid_prefix')"
interval="$(bashio::config 'scan_interval')"

if bashio::config.has_value 'mqtt_host'; then
    bashio::log.info "Using manually configured MQTT broker"
    export MQTT_HOST="$(bashio::config 'mqtt_host')"
    export MQTT_PORT="$(bashio::config 'mqtt_port' '1883')"
    export MQTT_USERNAME="$(bashio::config 'mqtt_username' '')"
    export MQTT_PASSWORD="$(bashio::config 'mqtt_password' '')"
elif bashio::services.available 'mqtt'; then
    bashio::log.info "Using MQTT broker provided by Home Assistant (Mosquitto add-on)"
    export MQTT_HOST="$(bashio::services 'mqtt' 'host')"
    export MQTT_PORT="$(bashio::services 'mqtt' 'port')"
    export MQTT_USERNAME="$(bashio::services 'mqtt' 'username')"
    export MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
else
    bashio::exit.nok "No MQTT broker found. Install the Mosquitto broker add-on or set mqtt_host in the configuration."
fi

bashio::log.info "Modem: ${MODEM_URL} | MQTT: ${MQTT_HOST}:${MQTT_PORT} | every ${interval}s"

while true; do
    if output="$(/usr/bin/technicolor_signal_dump.sh 2>&1)"; then
        if bashio::config.true 'verbose'; then
            bashio::log.info "${output}"
        else
            bashio::log.info "Modem scraped successfully"
        fi
    else
        rc=$?
        bashio::log.warning "Scrape failed (exit code ${rc}):"
        bashio::log.warning "${output}"
    fi
    sleep "${interval}"
done
