#!/bin/bash
# From the Mac: the Claude plan usage the server sees (5-hour and weekly utilisation and reset times), via
# usage-probe.sh on the server. Prints percentages and times only, never the token. Reads only.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'bash -s' <"$(dirname "$0")/usage-probe.sh"
