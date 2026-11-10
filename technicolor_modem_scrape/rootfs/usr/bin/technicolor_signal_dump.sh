#!/bin/bash

# technicolor_signal_dump.sh
# Scraper for Technicolor TC4400 cable modems -> MQTT (Home Assistant discovery)
#
# Based on technicolor_signal_dump.sh by Philipp Reichmuth
#   https://github.com/phrxmd/Technicolor_modem_scrape
# which is based on the Arris and Netgear scraper scripts by mmiller7.
#
# Copyright (c) 2023 Philipp Reichmuth (original scraper)
#
# License: CC-BY-NC-SA 4.0 (same as the original)
# https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode
#
# Changes vs. the original:
#  * All settings come from environment variables (or an optional config file
#    passed as $1), so the script never needs to be edited.
#  * Uses mosquitto_pub from PATH; no copying binaries into /config.
#  * Supports MQTT port, and brokers without authentication.
#  * DRY_RUN=1 prints MQTT messages instead of publishing them.
#  * Channel loops no longer run in a subshell, so MQTT errors abort properly.

# Optional config file (shell syntax, KEY="value")
if [ -n "$1" ] && [ -f "$1" ]; then
	# shellcheck disable=SC1090
	. "$1"
fi

# ---- Settings (environment overrides defaults) -----------------------------
modem_url="${MODEM_URL:-http://192.168.100.1}"
modem_username="${MODEM_USERNAME:-admin}"
modem_password="${MODEM_PASSWORD:-bEn2o#US9s}"   # TC4400 default admin password

mqtt_broker="${MQTT_HOST:-127.0.0.1}"
mqtt_port="${MQTT_PORT:-1883}"
mqtt_username="${MQTT_USERNAME:-}"
mqtt_password="${MQTT_PASSWORD:-}"

mqtt_topic_prefix="${MQTT_TOPIC_PREFIX:-homeassistant/sensor}"
mqtt_uid_prefix="${MQTT_UID_PREFIX:-cablemodem}"
mqtt_logging_topic="${MQTT_LOGGING_TOPIC:-${mqtt_topic_prefix}/technicolor_scraper}"

# Helper for writing to stderr
function echoerr() { echo -e "$@" 1>&2 ; }

# ---- MQTT publisher ---------------------------------------------------------
if [ "${DRY_RUN:-0}" != "1" ] && ! command -v mosquitto_pub &> /dev/null; then
	echoerr "Mosquitto MQTT client 'mosquitto_pub' not found (install package mosquitto-clients)"
	exit 10
fi

# Usage: mqtt_pub [-r] <topic> <message>
function mqtt_pub() {
	local retain=()
	if [ "$1" == "-r" ]; then retain=(-r); shift; fi
	local topic="$1" message="$2"
	if [ "${DRY_RUN:-0}" == "1" ]; then
		echo "[dry-run] ${retain[*]} ${topic}: ${message}"
		return 0
	fi
	local auth=()
	[ -n "$mqtt_username" ] && auth+=(-u "$mqtt_username")
	[ -n "$mqtt_password" ] && auth+=(-P "$mqtt_password")
	mosquitto_pub "${retain[@]}" -h "$mqtt_broker" -p "$mqtt_port" "${auth[@]}" -t "$topic" -m "$message"
}

#####################################
# Prep functions to interface modem #
#####################################

function setAuthHash() {
	auth_hash=$(echo -n "${modem_username}:${modem_password}" | base64 | tr -d '\n')
}

function loginStatus () {
	message="{ \"login\": \"$1\" }"
	echo "${mqtt_logging_topic}/login: ${message}"
	mqtt_pub "${mqtt_logging_topic}/login" "$message" || { echoerr "MQTT error posting logging message ${message} to topic ${mqtt_logging_topic}/login" ; exit 20 ; }
}

function fetchPage () {
	result=$(curl --connect-timeout 20 -s "${modem_url}/$1" -H 'Accept: */*' -H 'Content-Type: application/x-www-form-urlencoded; charset=utf-8' -H "Authorization: Basic ${auth_hash}" -H 'X-Requested-With: XMLHttpRequest' -H 'Cookie: HttpOnly: true, Secure: true') || { echoerr "curl error accessing ${modem_url}/$1" ; exit 22 ; }
}
function getSWInfo ()     { fetchPage "cmswinfo.html"; }
function getConnStatus () { fetchPage "cmconnectionstatus.html"; }

# Yes, the modem really says "Getway" in the title
title_sw='<title>Residential Getway Configuration: Status - Software</title>'
title_conn='<title>Residential Getway Configuration: Status - Connection</title>'

###############
# Preparation #
###############
setAuthHash

###################################
# Step 1: Cable modem information #
###################################
echo "Getting SW info"
getSWInfo

if [ "$(echo "$result" | grep -c "$title_sw")" == "0" ]; then
	echoerr "Got bad response retrieving software information page from modem. Retrying"
	echoerr "Result string: \n${result}"
	loginStatus "failed_retrying"
	getSWInfo
fi

if [ "$(echo "$result" | grep -c "$title_sw")" == "0" ]; then
	echoerr "Got bad response retrieving software information page on the second attempt. Exiting"
	echoerr "Result string: \n${result}"
	loginStatus "failed"
	exit 21
else
	loginStatus "success"
fi

####################
# Parse the result #
####################
sw_information=$(echo "$result" | tr '\n' ' ' | sed 's/\t//g;s/ //g;s/dBmV//g;s/dB//g;s/kHz/000/g;s/Hz//g;s/<[/]\?strong>//g;s/<![^>]*>//g;s/<[/]\?[bui]>//g' | awk -F "<tableborder=\"1\"cellpadding=\"3\"cellspacing=\"0\">|</table>" '{print $2}')
sw_status=$(echo "$result" | tr '\n' ' ' | sed 's/\t//g;s/ //g;s/dBmV//g;s/dB//g;s/kHz/000/g;s/Hz//g;s/<[/]\?strong>//g;s/<![^>]*>//g;s/<[/]\?[bui]>//g' | awk -F "<tableborder=\"1\"cellpadding=\"3\"cellspacing=\"0\">|</table>" '{print $4}')

sw_information_rows=$(echo "$sw_information" | sed 's/^<tr>//g;s/<\/tr>$//g;s/<\/tr><tr[^>]*>/\n/g')
sw_status_rows=$(echo "$sw_status" | sed 's/^<tr>//g;s/<\/tr>$//g;s/<\/tr><tr[^>]*>/\n/g')

cm_info=$(echo "$sw_information_rows\\n$sw_status_rows" | sed 's/<th[^>]*>[^<]*<\/th>//g;s/^<td[^>]*>//g;s/<\/td>$//g;s/<\/td><td[^>]*>/\t/g' | grep -v "^$" | tr '\n' '\t')

# Tab-delimited fields (awk index):
#  $2 DOCSIS compliance  $4 HW version  $6 SW version  $8 MAC  $10 serial
#  $14 uptime  $18 IPv4  $20 IPv6
serial_number=$(echo $cm_info | awk '{print $10}')
hw_version=$(echo $cm_info | awk '{print $4}')
sw_version=$(echo $cm_info | awk '{print $6}')
mac_address=$(echo $cm_info | awk '{print $8}')
uptime=$(echo $cm_info | awk '{print $14}' | sed 's/days/d /;s/:/ /g;s/ 0/ /g;')
addr_ipv4=$(echo $cm_info | awk '{print $18}' | sed 's/IPv4=//;')
addr_ipv6=$(echo $cm_info | awk '{print $20}' | sed 's/IPv6=//;')

echo "Serial number: $serial_number"
echo "Hardware version: $hw_version"
echo "Software version: $sw_version"
echo "Modem MAC Address: $mac_address"
echo "Modem IPv4 Address: $addr_ipv4"
echo "Modem IPv6 Address: $addr_ipv6"
echo "Uptime: $uptime"

if [ -z "$serial_number" ]; then
	echoerr "Could not parse the modem serial number - unsupported firmware/page layout?"
	exit 23
fi

modem_id="$mqtt_uid_prefix$serial_number"
mqtt_topic="$mqtt_topic_prefix/$modem_id"

#############################
# Step 2: Connection status #
#############################
echo "Getting connection status"
getConnStatus

if [ "$(echo "$result" | grep -c "$title_conn")" == "0" ]; then
	echoerr "Got bad response retrieving connection status from modem. Retrying"
	echoerr "Result string: \n${result}"
	loginStatus "failed_retrying"
	getConnStatus
fi

if [ "$(echo "$result" | grep -c "$title_conn")" == "0" ]; then
	echoerr "Got bad response retrieving connection status on the second attempt. Exiting"
	echoerr "Result string: \n${result}"
	loginStatus "failed"
	exit 21
else
	loginStatus "success"
fi

# Publish a Home Assistant MQTT discovery config message for one sub-sensor
# Args: name state_topic infix suffix [suffix_descriptive] [state_class] [unit] [device_class]
function pubConfigMessage () {
	config_name="$1"
	config_state_topic="$2"
	config_infix="$3"
	config_suffix="$4"
	config_suffix_descriptive="$5"
	config_state_class="$6"
	config_unit="$7"
	config_device_class="$8"

	config_topic="${config_state_topic}_${config_suffix}/config"

	config_message="\"name\": \"${config_name}"
	[ -n "${config_suffix_descriptive}" ] && config_message="${config_message} ${config_suffix_descriptive}"
	config_message="${config_message}\", "
	config_message="${config_message} \"state_topic\": \"${config_state_topic}\", "
	config_message="${config_message} \"unique_id\": \"${modem_id}_${config_infix}_${config_suffix}\", "
	config_message="${config_message} \"value_template\": \"{{ value_json.${config_suffix} }}\", "
	[ -n "${config_device_class}" ] && config_message="${config_message} \"device_class\": \"${config_device_class}\", "
	[ -n "${config_state_class}" ]  && config_message="${config_message} \"state_class\": \"${config_state_class}\", "
	[ -n "${config_unit}" ]         && config_message="${config_message} \"unit_of_measurement\": \"${config_unit}\", "
	config_message="${config_message} \"device\": {"
	config_message="${config_message} \"identifiers\": [\"${serial_number}\",\"${modem_id}\"], "
	config_message="${config_message} \"manufacturer\": \"Technicolor\", "
	config_message="${config_message} \"model\": \"TC4400\", "
	config_message="${config_message} \"name\": \"TC4400\", "
	config_message="${config_message} \"configuration_url\": \"${modem_url}\", "
	config_message="${config_message} \"hw_version\": \"${hw_version}\", "
	config_message="${config_message} \"sw_version\": \"${sw_version}\", "
	config_message="${config_message} \"connections\": [[\"mac\", \"${mac_address}\"]]"
	config_message="${config_message} }"
	config_message="{ ${config_message} }"

	# Retained, so the device doesn't disappear when HA restarts
	mqtt_pub -r "${config_topic}" "${config_message}" || { echoerr "MQTT error posting config message to topic ${config_topic}" ; exit 20 ; }
}

####################
# Parse the result #
####################
startup_status=$(echo "$result" | tr '\n' ' ' | sed 's/\t//g;s/ //g;s/dBmV//g;s/dB//g;s/kHz/000/g;s/Hz//g;s/<[/]\?strong>//g;s/<![^>]*>//g;s/<[/]\?[bui]>//g' | awk -F "<tableborder=\"1\"cellpadding=\"4\"cellspacing=\"0\">|</table>" '{print $2}')
downstream_status=$(echo "$result" | tr '\n' ' ' | sed 's/\t//g;s/ //g;s/dBmV//g;s/dB//g;s/kHz/000/g;s/Hz//g;s/<[/]\?strong>//g;s/<![^>]*>//g' | awk -F "<tableborder='1'cellpadding='4'cellspacing='0'>|</table>" '{print $3}')
upstream_status=$(echo "$result" | tr '\n' ' ' | sed 's/\t//g;s/ //g;s/dBmV//g;s/dB//g;s/kHz/000/g;s/Hz//g;s/<[/]\?strong>//g;s/<![^>]*>//g' | awk -F "<tableborder='1'cellpadding='4'cellspacing='0'>|</table>" '{print $5}')

startup_rows=$(echo "$startup_status" | sed 's/^<tr>//g;s/<\/tr>$//g;s/<\/tr><tr[^>]*>/\n/g')
downstream_rows=$(echo "$downstream_status" | sed 's/^<tr>//g;s/<\/tr>$//g;s/<\/tr><tr[^>]*>/\n/g')
upstream_rows=$(echo "$upstream_status" | sed 's/^<tr>//g;s/<\/tr>$//g;s/<\/tr><tr[^>]*>/\n/g')

echo "Parsing startup status"
to_parse=$(echo "$startup_rows" | sed 's/<th[^>]*>[^<]*<\/th>//g;s/^<td[^>]*>//g;s/<\/td>$//g;s/<\/td><td[^>]*>/\t/g' | grep -v "^$" | tr '\n' '\t')
# $5 downstream freq, $6 locked, $9 connectivity, $12 boot, $15 config file, $17/$18 security

state_topic="${mqtt_topic}/status"
pubConfigMessage "Modem" ${state_topic} "status" "channel" "Channel State"
pubConfigMessage "Modem" ${state_topic} "status" "frequency" "Downstream Channel" "measurement" "Hz" "frequency"
pubConfigMessage "Modem" ${state_topic} "status" "connectivity" "Connectivity State"
pubConfigMessage "Modem" ${state_topic} "status" "boot" "Boot State"
pubConfigMessage "Modem" ${state_topic} "status" "security" "Security State"
pubConfigMessage "Modem" ${state_topic} "status" "secservice" "Security Service"
pubConfigMessage "Modem" ${state_topic} "status" "configfile" "Configuration File Hash"
pubConfigMessage "Modem" ${state_topic} "status" "uptime" "Uptime"
pubConfigMessage "Modem" ${state_topic} "status" "addr_ipv4" "IPv4 Address"
pubConfigMessage "Modem" ${state_topic} "status" "addr_ipv6" "IPv6 Address"

state_message=""
state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"channel\": \""$6"\", " }')"
state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"frequency\": "$5", " }')"
state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"connectivity\": \""$9"\", " }')"
state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"boot\": \""$12"\", " }')"
state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"security\": \""$17"\", " }')"
state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"secservice\": \""$18"\", " }')"
state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"configfile\": \""$15"\", " }')"
state_message="${state_message}\"uptime\": \"${uptime}\", "
state_message="${state_message}\"addr_ipv4\": \"${addr_ipv4}\", "
state_message="${state_message}\"addr_ipv6\": \"${addr_ipv6}\""
state_message="{ ${state_message} }"
mqtt_pub "${state_topic}" "${state_message}" || { echoerr "MQTT error posting modem status to topic ${state_topic}" ; exit 20 ; }

echo "Parsing downstream channels"
while read -r line; do
	[ -z "$line" ] && continue
	to_parse=$(echo "$line" | sed 's/<th[^>]*>[^<]*<\/th><\/tr>//g;s/^<td>//g;s/<\/td>$//g;s/<\/td><td[^>]*>/\t/g')
	# $1 channel $2 id $3 lock $4 type $5 bonding $6 freq $7 width $8 snr $9 power
	# $10 profile $11 unerrored $12 corrected $13 uncorrectable

	# Sensor IDs follow the channel ID, independent of the ordering on the page
	index=$(echo $to_parse | awk '{print $2}')
	state_topic="${mqtt_topic}/downstream${index}"

	echo "Publishing downstream channel ${index}"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "id" "ID"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "type" "Type"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "frequency" "Frequency" "measurement" "Hz" "frequency"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "width" "Width" "measurement" "Hz" "frequency"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "power" "Power" "measurement" "dBmV"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "profile" "Modulation Profile"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "lockstatus" "Lock Status"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "bondingstatus" "Bonding Status"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "snr" "SNR" "measurement" "dB" "signal_strength"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "noerr" "Error-free" "total_increasing" "B" "data_size"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "corr" "Corrected" "total_increasing" "B" "data_size"
	pubConfigMessage "Down ${index}" ${state_topic} "downstream${index}" "uncorr" "Uncorrectable" "total_increasing" "B" "data_size"

	state_message=""
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"id\": "$2", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"type\": \""$4"\", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"frequency\": "$6", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"width\": "$7", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"snr\": "$8", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"power\": "$9", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"profile\": \""$10"\", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"lockstatus\": \""$3"\", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"bondingstatus\": \""$5"\", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"noerr\": "$11", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"corr\": "$12", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"uncorr\": "$13 }')"
	state_message="{ ${state_message} }"

	mqtt_pub "${state_topic}" "${state_message}" || { echoerr "MQTT error posting downstream channel status to topic ${state_topic}" ; exit 20 ; }
done < <(echo "$downstream_rows" | tail -n +3)

echo "Parsing upstream channels"
counter=0
while read -r line; do
	[ -z "$line" ] && continue
	counter=$((counter+1))
	to_parse=$(echo "$line" | sed 's/<th[^>]*>[^<]*<\/th><\/tr>//g;s/^<td>//g;s/<\/td>$//g;s/<\/td><td[^>]*>/\t/g')
	# $1 channel $2 id $3 lock $4 type $5 bonding $6 freq $7 width $8 tx level $9 profile

	index=${counter}
	state_topic="${mqtt_topic}/upstream${index}"

	echo "Publishing upstream channel ${index}"
	pubConfigMessage "Up ${index}" ${state_topic} "upstream${index}" "id" "ID"
	pubConfigMessage "Up ${index}" ${state_topic} "upstream${index}" "type" "Type"
	pubConfigMessage "Up ${index}" ${state_topic} "upstream${index}" "frequency" "Frequency" "measurement" "Hz" "frequency"
	pubConfigMessage "Up ${index}" ${state_topic} "upstream${index}" "width" "Width" "measurement" "Hz" "frequency"
	pubConfigMessage "Up ${index}" ${state_topic} "upstream${index}" "power" "Power" "measurement" "dBmV"
	pubConfigMessage "Up ${index}" ${state_topic} "upstream${index}" "profile" "Modulation Profile"
	pubConfigMessage "Up ${index}" ${state_topic} "upstream${index}" "lockstatus" "Lock Status"
	pubConfigMessage "Up ${index}" ${state_topic} "upstream${index}" "bondingstatus" "Bonding Status"

	state_message=""
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"id\": "$2", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"type\": \""$4"\", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"frequency\": "$6", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"width\": "$7", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"power\": "$8", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"profile\": \""$9"\", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"lockstatus\": \""$3"\", " }')"
	state_message="${state_message}$(echo "$to_parse" | awk '{ print "\"bondingstatus\": \""$5"\"" }')"
	state_message="{ ${state_message} }"

	mqtt_pub "${state_topic}" "${state_message}" || { echoerr "MQTT error posting upstream channel status to topic ${state_topic}" ; exit 20 ; }
done < <(echo "$upstream_rows" | tail -n +3)

echo "Done."
