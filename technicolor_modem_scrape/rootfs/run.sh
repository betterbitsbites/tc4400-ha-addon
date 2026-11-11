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
export HISTORY_LENGTH="$(bashio::config 'history_length' '30')"
# counters and channel history survive app restarts
export STATE_DIR="/data"

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

# Dashboard generator: listens for the "Create Dashboard" button (or mqtt.publish)
# and for the "Dashboard Target" select
export DASHBOARD_COMMAND_TOPIC="tc4400_scraper/dashboard/create"
DASHBOARD_TARGET_TOPIC="tc4400_scraper/dashboard/target/set"
POWER_SWITCH_TOPIC="tc4400_scraper/power/switch/set"
POWER_SENSOR_TOPIC="tc4400_scraper/power/sensor/set"
RESTART_TOPIC="tc4400_scraper/modem/restart"
dashboard() {
    python3 /usr/bin/tc4400_dashboard.py "$@" 2>&1 |
        while IFS= read -r line; do bashio::log.info "[dashboard] ${line}"; done
    return "${PIPESTATUS[0]}"
}
dashboard_listener() {
    local auth=()
    [ -n "${MQTT_USERNAME}" ] && auth+=(-u "${MQTT_USERNAME}")
    [ -n "${MQTT_PASSWORD}" ] && auth+=(-P "${MQTT_PASSWORD}")
    while true; do
        # -R: ignore retained messages, so a dashboard is only built on request
        # -v: prefix every message with its topic
        mosquitto_sub -R -v -h "${MQTT_HOST}" -p "${MQTT_PORT}" "${auth[@]}" \
            -t "${DASHBOARD_COMMAND_TOPIC}" -t "${DASHBOARD_TARGET_TOPIC}" \
            -t "${POWER_SWITCH_TOPIC}" -t "${POWER_SENSOR_TOPIC}" -t "${RESTART_TOPIC}" |
        while IFS= read -r line; do
            topic="${line%% *}"
            payload=""
            [ "${line}" != "${topic}" ] && payload="${line#* }"
            if [ "${topic}" == "${DASHBOARD_TARGET_TOPIC}" ]; then
                dashboard select "${payload}"
            elif [ "${topic}" == "${POWER_SWITCH_TOPIC}" ]; then
                dashboard power-switch "${payload}"
            elif [ "${topic}" == "${POWER_SENSOR_TOPIC}" ]; then
                dashboard power-sensor "${payload}"
            elif [ "${topic}" == "${RESTART_TOPIC}" ]; then
                bashio::log.info "Modem restart requested"
                dashboard restart &   # takes a while; the cooldown prevents double restarts
            else
                bashio::log.info "Dashboard requested"
                dashboard create "${payload}"
            fi
        done
        bashio::log.warning "Dashboard listener lost the MQTT connection, retrying in 30 s"
        sleep 30
    done
}
dashboard_listener &

# Follow the modem power switch: records every power cycle (button, automation,
# user, physical button) as "Last Power Cycle"
(
    sleep 60
    while true; do
        dashboard watch
        sleep 30
    done
) &

# Keep the list of dashboards in the "Dashboard Target" select up to date
# and publish the power switch/sensor dropdowns, Restart Modem and Last Power Cycle
(
    sleep 20
    # until it works once (the first scrape must have registered the device)
    for _ in $(seq 1 40); do
        dashboard refresh && break
        sleep 30
    done
    while true; do
        sleep 600
        dashboard refresh
    done
) &

# Internet latency probe (pings the configured targets, publishes rolling stats)
if bashio::config.true 'latency_enabled'; then
    (
        while true; do
            python3 /usr/bin/latency_probe.py 2>&1 | while IFS= read -r line; do bashio::log.info "${line}"; done
            bashio::log.warning "Latency probe stopped, restarting in 30 s"
            sleep 30
        done
    ) &
fi

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
