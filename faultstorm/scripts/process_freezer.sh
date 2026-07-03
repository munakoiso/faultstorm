#!/bin/bash
#
# process_freezer.sh — daemon that freezes random processes with SIGSTOP/SIGCONT.
#
# Runs as a long-lived process (e.g. under supervisor). Every POLL_INTERVAL
# seconds it checks whether the flag file exists. When the flag is detected,
# the daemon reads configuration and process name patterns from it, finds
# matching PIDs, picks a random one, and enters a SIGSTOP/SIGCONT loop.
# When the flag is removed (heal), the loop exits and the daemon goes back
# to polling.
#
# Flag file format — two sections separated by a ``[patterns]`` header:
#
#   [config]
#   freeze_min_ms=100
#   freeze_max_ms=3000
#   pause_min_ms=100
#   pause_max_ms=3000
#   [patterns]
#   postgres
#   pgconsul
#
# Config keys (all in milliseconds):
#   freeze_min_ms / freeze_max_ms — SIGSTOP hold duration range (default: 100 / 3000)
#   pause_min_ms  / pause_max_ms  — pause between freeze cycles  (default: 100 / 3000)
#
# Environment variables:
#   FREEZER_FLAG_FILE  — path to the flag file (default: /tmp/.process_freezer.flag)
#   FREEZER_LOG_FILE   — path to the log file (default: /var/log/process_freezer.log)
#   FREEZER_POLL_INTERVAL — poll interval in seconds (default: 1)

set -euo pipefail

FLAG_FILE="${FREEZER_FLAG_FILE:-/tmp/.process_freezer.flag}"
LOG_FILE="${FREEZER_LOG_FILE:-/var/log/process_freezer.log}"
POLL_INTERVAL="${FREEZER_POLL_INTERVAL:-1}"

# Defaults (milliseconds)
FREEZE_MIN_MS=100
FREEZE_MAX_MS=3000
PAUSE_MIN_MS=100
PAUSE_MAX_MS=3000

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [process_freezer] $*" >> "$LOG_FILE"
}

# Generate a random delay (in seconds, with ms precision) within a given range.
# Args: min_ms max_ms
random_delay() {
    local min_ms="$1"
    local max_ms="$2"
    local range=$((max_ms - min_ms))
    local delay_ms
    if [ "$range" -le 0 ]; then
        delay_ms="$min_ms"
    else
        delay_ms=$(( (RANDOM % range) + min_ms ))
    fi
    local secs=$((delay_ms / 1000))
    local msecs=$((delay_ms % 1000))
    printf "%d.%03d" "$secs" "$msecs"
}

# Get full command line for a PID (like ps -x shows).
# Returns the full args string or "unknown" if the process is gone.
get_full_cmdline() {
    local pid="$1"
    ps -p "$pid" -o args= 2>/dev/null || echo "unknown"
}

# Find PIDs matching any of the given process name patterns.
# Args: pattern1 pattern2 ...
# Prints matching PIDs, one per line.
find_matching_pids() {
    local pids=""
    for pattern in "$@"; do
        # Use pgrep -f to match against full command line.
        local found
        found=$(pgrep -f "$pattern" 2>/dev/null || true)
        if [ -n "$found" ]; then
            pids="$pids $found"
        fi
    done
    # Deduplicate and exclude our own process tree
    local my_pid=$$
    for pid in $pids; do
        # Skip our own PID and PID 1 (init)
        if [ "$pid" != "$my_pid" ] && [ "$pid" != "1" ]; then
            # Verify the process still exists
            if kill -0 "$pid" 2>/dev/null; then
                echo "$pid"
            fi
        fi
    done | sort -u
}

# Pick a random element from stdin lines
pick_random() {
    local lines=()
    while IFS= read -r line; do
        lines+=("$line")
    done
    local count=${#lines[@]}
    if [ "$count" -eq 0 ]; then
        return 1
    fi
    local idx=$((RANDOM % count))
    echo "${lines[$idx]}"
}

# Parse the flag file into config variables and patterns array.
# Sets global: FREEZE_MIN_MS, FREEZE_MAX_MS, PAUSE_MIN_MS, PAUSE_MAX_MS
# Populates the array whose name is passed as $1 with patterns.
parse_flag_file() {
    local -n _patterns=$1
    _patterns=()

    local section="config"
    while IFS= read -r line; do
        # Strip leading/trailing whitespace
        line=$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [ -z "$line" ] && continue

        # Section headers
        if [ "$line" = "[config]" ]; then
            section="config"
            continue
        fi
        if [ "$line" = "[patterns]" ]; then
            section="patterns"
            continue
        fi

        if [ "$section" = "config" ]; then
            # Parse key=value
            local key="${line%%=*}"
            local val="${line#*=}"
            case "$key" in
                freeze_min_ms) FREEZE_MIN_MS="$val" ;;
                freeze_max_ms) FREEZE_MAX_MS="$val" ;;
                pause_min_ms)  PAUSE_MIN_MS="$val" ;;
                pause_max_ms)  PAUSE_MAX_MS="$val" ;;
                *) log "WARNING: unknown config key '$key'" ;;
            esac
        elif [ "$section" = "patterns" ]; then
            _patterns+=("$line")
        fi
    done < "$FLAG_FILE"
}

# Main freeze loop: runs while the flag file exists
freeze_loop() {
    # Reset to defaults before parsing (in case previous run changed them)
    FREEZE_MIN_MS=100
    FREEZE_MAX_MS=3000
    PAUSE_MIN_MS=100
    PAUSE_MAX_MS=3000

    local patterns=()
    parse_flag_file patterns

    if [ ${#patterns[@]} -eq 0 ]; then
        log "WARNING: flag file has no patterns to match"
        return
    fi

    log "Freeze activated with patterns: ${patterns[*]}"
    log "Config: freeze=${FREEZE_MIN_MS}-${FREEZE_MAX_MS}ms, pause=${PAUSE_MIN_MS}-${PAUSE_MAX_MS}ms"

    while [ -f "$FLAG_FILE" ]; do
        # Find matching PIDs
        local pids
        pids=$(find_matching_pids "${patterns[@]}")

        if [ -z "$pids" ]; then
            log "No matching processes found for patterns: ${patterns[*]}"
            sleep "$POLL_INTERVAL"
            continue
        fi

        # Pick a random PID
        local target_pid
        target_pid=$(echo "$pids" | pick_random) || {
            log "Failed to pick a random PID"
            sleep "$POLL_INTERVAL"
            continue
        }

        # Get full command line for logging
        local full_cmd
        full_cmd=$(get_full_cmdline "$target_pid")

        # Verify the process still exists before freezing
        if ! kill -0 "$target_pid" 2>/dev/null; then
            log "Process $target_pid no longer exists, skipping (was: $full_cmd)"
            continue
        fi

        # Send SIGSTOP
        local delay
        delay=$(random_delay "$FREEZE_MIN_MS" "$FREEZE_MAX_MS")
        log "SIGSTOP -> PID $target_pid ($full_cmd), will hold for ${delay}s"
        if kill -STOP "$target_pid" 2>/dev/null; then
            # Wait random time
            sleep "$delay"

            # Send SIGCONT (even if flag was removed — always unfreeze)
            log "SIGCONT -> PID $target_pid ($full_cmd)"
            kill -CONT "$target_pid" 2>/dev/null || true
        else
            log "Failed to SIGSTOP PID $target_pid, process may have exited (was: $full_cmd)"
        fi

        # Pause before next iteration
        delay=$(random_delay "$PAUSE_MIN_MS" "$PAUSE_MAX_MS")
        log "Waiting ${delay}s before next freeze cycle"
        sleep "$delay"
    done

    log "Flag file removed, freeze deactivated"
}

# Cleanup: make sure no processes are left stopped
cleanup() {
    log "Daemon shutting down, cleaning up..."
    # Try to SIGCONT any processes that might be stopped
    # This is a best-effort safety net
    if [ -f "$FLAG_FILE" ]; then
        local patterns=()
        parse_flag_file patterns

        if [ ${#patterns[@]} -gt 0 ]; then
            local pids
            pids=$(find_matching_pids "${patterns[@]}")
            for pid in $pids; do
                kill -CONT "$pid" 2>/dev/null || true
            done
            log "Sent SIGCONT to all matching processes as safety cleanup"
        fi
    fi
    exit 0
}

trap cleanup SIGTERM SIGINT

# Main daemon loop
log "Process freezer daemon started (poll interval: ${POLL_INTERVAL}s)"

while true; do
    if [ -f "$FLAG_FILE" ]; then
        freeze_loop
    fi
    sleep "$POLL_INTERVAL"
done
