#!/bin/bash

# Keeps the coding-agent tools a user installed current, and installs one only when it is named.
# These tools live in the user's own home and each carries its own updater, so this script runs as that user, never as root, and drives each tool's updater rather than replacing it.
# An upgrade touches only a tool already present, so a host where nobody installed codex never gets codex from a nightly run.
#
# A tool installed through a package manager (npm, Homebrew, mise, Nix, apt) is reported and left to that manager, since running the tool's own updater over it would fight the manager for the same files.
# Every step is idempotent, and a tool that fails is reported while the rest still run.

set -Eeuo pipefail

# The managed agent tools, in no particular order, since none depends on another.
readonly TOOLS=(claude codex opencode herdr cswap)

# Seconds one updater or installer may run before it is treated as hung, long enough for a slow download and short enough that a nightly run ends.
readonly STEP_TIMEOUT=900

MODE="report"
DRY_RUN=false
JSON_OUTPUT=false
ASSUME_YES=false
INSTALL_ALL=false
SELECTED=()
REQUESTED=()

NOTES=()
NOTE_TEXTS=()
FAILED=()
CHANGED=()

TMP_DIR=$(mktemp -d)
readonly TMP_DIR
trap 'rm -rf "$TMP_DIR"' EXIT

# --- Output ---

log() { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    NOTES+=("$1: $2")
    NOTE_TEXTS+=("$2")
}

hide_home() {
    local path="$1"
    if [[ -n ${HOME:-} && ($path == "$HOME" || $path == "$HOME/"*) ]]; then
        printf '~%s' "${path#"$HOME"}"
    else
        printf '%s' "$path"
    fi
}

# Every string this writes is a tool name, a version, a path, or a note this script composed, so escaping the two JSON metacharacters and control bytes is the whole job.
json_string() {
    local s="$1" out="" i c
    for ((i = 0; i < ${#s}; i++)); do
        c="${s:i:1}"
        case "$c" in
        \" | \\) out+="\\$c" ;;
        [[:cntrl:]]) out+=$(printf '\\u%04x' "'$c") ;;
        *) out+="$c" ;;
        esac
    done
    printf '"%s"' "$out"
}

json_value() {
    if [[ -n $1 ]]; then
        json_string "$1"
    else
        printf 'null'
    fi
}

json_notes() {
    local i sep=""
    printf '['
    for ((i = $1; i < $2; i++)); do
        printf '%s' "$sep"
        json_string "${NOTE_TEXTS[i]}"
        sep=", "
    done
    printf ']'
}

usage() {
    cat <<'EOF'
Usage: install-agents.sh [options] [tool ...]

Keeps the coding-agent tools this user installed current, through each tool's own updater, and
installs a tool only when it is named. Runs as the user who owns the tools, never as root.

Actions, name one, default --report:
  -r, --report      Report each tool, where it lives, and what manages it, change nothing
  -u, --upgrade     Upgrade each tool already installed, and install nothing
  -i, --install     Install the named tools that are missing, leave an installed one alone
  -l, --list        List the managed tools and where each installs from
  -h, --help        Show this help

Options:
  -a, --all         With --install, name every managed tool
  -j, --json        Write the report as JSON rather than a table, for a program to read
  -n, --dry-run     Print the commands instead of running them
  -y, --yes         Do not prompt before changing anything

With no tool named, --report and --upgrade select every managed tool, while --install selects
none, so an install always names what it installs, or says --all.

A tool reads as "self" when it was installed by its own installer and so updates itself through
this script, and as the name of a package manager when one owns it, which --upgrade leaves to
that manager. herdr's agent integrations are reinstalled after an upgrade wherever herdr reports
one outdated, and a herdr server still running the previous binary is left running and reported
as needing a restart, since stopping it would end every pane inside it.

--json writes one object: "schema" (1), "platform" ("linux"), "tools", one entry per tool with
"tool", "installed", "path", "manager", "status" ("present" or "absent"), "restart_pending" (true,
false, or null where the tool cannot say) and that tool's own "notes", and a top-level "notes".

Examples:
  install-agents.sh                        Report on every tool
  install-agents.sh --upgrade --yes        Upgrade every tool already installed, no prompt
  install-agents.sh --upgrade herdr        Upgrade herdr, if it is installed
  install-agents.sh --install herdr cswap  Install two tools
  install-agents.sh --install --all        Install every tool that is missing
EOF
}

# --- Execution ---

# Run a command, or print it under --dry-run.
# A read used to decide what to do runs either way, and only a command that changes something goes through here.
# Standard input is closed and the time bounded, so an updater that stops to ask a question fails rather than hanging a nightly run.
run() {
    if [[ $DRY_RUN == true ]]; then
        printf '  [dry run] %s\n' "$*"
        return 0
    fi
    timeout "$STEP_TIMEOUT" "$@" </dev/null
}

confirm() {
    [[ $ASSUME_YES == true || $DRY_RUN == true ]] && return 0
    [[ -t 0 ]] || die "Not a terminal and --yes was not given, refusing to change anything unattended"
    local reply
    read -r -p "$1 [y/N] " reply
    [[ $reply == [yY] || $reply == [yY][eE][sS] ]]
}

# Fetches an installer to a file and runs it from there rather than piping it into a shell, so a truncated download fails before it runs rather than part way through.
run_installer() {
    local url="$1" shell="$2" file
    file="$TMP_DIR/$(basename "$url").installer"
    if [[ $DRY_RUN == true ]]; then
        printf '  [dry run] curl -fsSL %s | %s\n' "$url" "$shell"
        return 0
    fi
    command -v curl >/dev/null || {
        warn "curl is not installed, so $url cannot be fetched"
        return 1
    }
    curl -fsSL --proto '=https' --tlsv1.2 --retry 2 --connect-timeout 15 "$url" -o "$file" || return 1
    timeout "$STEP_TIMEOUT" "$shell" "$file" </dev/null
}

# A nightly run reaches this through runuser or a systemd unit, whose PATH carries none of the per-user directories these installers write to.
# Each is appended rather than prepended, so a caller whose PATH already orders them keeps its own order.
extend_path() {
    local dir
    for dir in "$HOME/.local/bin" "$HOME/.opencode/bin"; do
        [[ -d $dir && ":$PATH:" != *":$dir:"* ]] && PATH="$PATH:$dir"
    done
    export PATH
}

# --- Detection ---

tool_function() {
    printf '%s_%s' "$1" "$2"
}

tool_path() {
    command -v "$1" 2>/dev/null || true
}

# The first dotted version in the tool's own banner, since each prints it with different words around it.
tool_version() {
    local path="$1" out
    out=$(timeout 30 "$path" --version 2>/dev/null </dev/null) || true
    [[ $out =~ ([0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*) ]] && printf '%s' "${BASH_REMATCH[1]}"
    return 0
}

# What owns the installed copy, read from where the command resolves to rather than from how it was meant to be installed.
tool_manager() {
    local path="$1" real
    real=$(readlink -f -- "$path" 2>/dev/null) || real="$path"
    case "$real" in
    */.bun/*) printf 'bun' ;;
    */node_modules/*) printf 'npm' ;;
    */linuxbrew/* | /opt/homebrew/* | */Cellar/* | */Caskroom/*) printf 'brew' ;;
    */mise/installs/*) printf 'mise' ;;
    /nix/store/*) printf 'nix' ;;
    *)
        if [[ $real == /usr/* && $real != /usr/local/* ]] && command -v dpkg >/dev/null && dpkg -S "$real" >/dev/null 2>&1; then
            printf 'apt'
        else
            printf 'self'
        fi
        ;;
    esac
}

# --- claude ---

claude_source() { printf 'https://claude.ai/install.sh'; }
claude_upgrade() { run claude update; }
claude_install() { run_installer "$(claude_source)" bash; }

# --- codex ---

codex_source() { printf 'https://chatgpt.com/codex/install.sh'; }
codex_upgrade() { run codex update; }
codex_install() { run_installer "$(codex_source)" sh; }

# --- opencode ---

opencode_source() { printf 'https://opencode.ai/install'; }
opencode_upgrade() { run opencode upgrade; }
opencode_install() { run_installer "$(opencode_source)" bash; }

# --- herdr ---

herdr_source() { printf 'https://herdr.dev/install.sh'; }

# A plain update rather than --handoff, which is experimental, so a live server keeps its panes on the old binary until someone restarts it.
herdr_upgrade() { run herdr update; }
herdr_install() { run_installer "$(herdr_source)" sh; }

# The integrations herdr reports outdated, one name per line.
herdr_outdated_integrations() {
    local out line
    out=$(timeout 30 herdr integration status --outdated-only 2>/dev/null </dev/null) || return 1
    while IFS= read -r line; do
        [[ $line =~ ^([A-Za-z0-9_-]+):[[:space:]] ]] && printf '%s\n' "${BASH_REMATCH[1]}"
    done <<<"$out"
    return 0
}

# Read from herdr's own status rather than from whether a session is open, since a session on the current binary needs no restart however many panes it holds.
# The pattern reads the "update" object alone, so a host without jq still gets an answer.
herdr_restart_pending() {
    local out
    out=$(timeout 30 herdr status --json 2>/dev/null </dev/null) || return 2
    if [[ $out =~ \"update\":\{[^}]*\"restart_needed\":true ]]; then
        return 0
    fi
    return 1
}

herdr_post() {
    local name names failed=0
    if ! names=$(herdr_outdated_integrations); then
        warn "herdr: could not read the integration status"
        return 1
    fi
    for name in $names; do
        log "herdr: reinstalling the outdated $name integration"
        run herdr integration install "$name" || failed=1
    done
    return "$failed"
}

herdr_note() {
    local names
    if ! names=$(herdr_outdated_integrations); then
        note herdr "could not read the integration status"
    elif [[ -n $names ]]; then
        note herdr "integrations outdated: ${names//$'\n'/ }, --upgrade reinstalls them"
    fi
    if herdr_restart_pending; then
        note herdr "a running server is on the previous binary, restart it when no pane needs it: herdr server stop"
    fi
}

# --- cswap ---

cswap_source() { printf 'uv tool install claude-swap'; }
cswap_upgrade() { run cswap upgrade; }

cswap_install() {
    [[ $DRY_RUN == true ]] || command -v uv >/dev/null || {
        warn "cswap installs as a uv tool, and uv is not installed, install-tools.sh installs it"
        return 1
    }
    run uv tool install claude-swap
}

# --- Report ---

restart_pending_json() {
    local tool="$1" rc=0
    if [[ $tool != "herdr" ]]; then
        printf 'null'
        return 0
    fi
    herdr_restart_pending || rc=$?
    case $rc in
    0) printf 'true' ;;
    1) printf 'false' ;;
    *) printf 'null' ;;
    esac
}

report() {
    local format="%-10s %-16s %-8s %s\n"
    local -a rows=()
    if [[ $JSON_OUTPUT == false ]]; then
        # shellcheck disable=SC2059  # Format string is a constant defined above.
        printf "$format" "TOOL" "INSTALLED" "MANAGER" "PATH"
    fi

    local tool path installed manager status restart first
    for tool in "${SELECTED[@]}"; do
        first=${#NOTES[@]}
        path=$(tool_path "$tool")
        installed="" manager="" status="absent" restart="null"
        if [[ -n $path ]]; then
            installed=$(tool_version "$path")
            manager=$(tool_manager "$path")
            status="present"
            if [[ $manager != "self" ]]; then
                note "$tool" "installed through $manager, which upgrades it, so --upgrade leaves it alone"
            elif [[ $tool == "herdr" ]]; then
                herdr_note
            fi
            restart=$(restart_pending_json "$tool")
        fi
        if [[ $JSON_OUTPUT == false ]]; then
            local shown_path="-"
            [[ -z $path ]] || shown_path=$(hide_home "$path")
            # shellcheck disable=SC2059  # Format string is a constant defined above.
            printf "$format" "$tool" "${installed:--}" "${manager:--}" "$shown_path"
        else
            rows+=("$(printf '{"tool": %s, "installed": %s, "path": %s, "manager": %s, "status": %s, "restart_pending": %s, "notes": %s}' \
                "$(json_string "$tool")" "$(json_value "$installed")" "$(json_value "$([[ -z $path ]] || hide_home "$path")")" \
                "$(json_value "$manager")" "$(json_string "$status")" "$restart" "$(json_notes "$first" "${#NOTES[@]}")")")
        fi
    done

    if [[ $JSON_OUTPUT == true ]]; then
        local i sep=""
        printf '{\n  "schema": 1,\n  "platform": "linux",\n  "tools": ['
        for ((i = 0; i < ${#rows[@]}; i++)); do
            printf '%s\n    %s' "$sep" "${rows[i]}"
            sep=","
        done
        [[ ${#rows[@]} -eq 0 ]] || printf '\n  '
        printf '],\n  "notes": []\n}\n'
        return 0
    fi

    [[ ${#NOTES[@]} -eq 0 ]] && return 0
    log ""
    log "Notes:"
    local entry
    for entry in "${NOTES[@]}"; do
        info "$entry"
    done
}

# --- Apply ---

upgrade_tool() {
    local tool="$1" path installed manager now
    path=$(tool_path "$tool")
    if [[ -z $path ]]; then
        log "$tool: not installed, leaving it absent"
        return 0
    fi
    manager=$(tool_manager "$path")
    installed=$(tool_version "$path")
    if [[ $manager != "self" ]]; then
        log "$tool: installed through $manager, leaving it to $manager"
        return 0
    fi

    # The herdr updater refuses to run from inside one of its own panes, so a run there skips that one step rather than failing on a refusal it already knows is coming.
    if [[ $tool == "herdr" && -n ${HERDR_PANE_ID:-} ]]; then
        log "herdr: at ${installed:-an unreadable version}, and this run is inside a herdr pane, which herdr will not update from, run this outside herdr to update it"
        herdr_post || FAILED+=("herdr integrations")
        return 0
    fi

    log "$tool: at ${installed:-an unreadable version}, running its updater"
    if ! "$(tool_function "$tool" upgrade)"; then
        warn "$tool: its updater failed"
        FAILED+=("$tool")
        return 0
    fi
    now=$(tool_version "$path")
    [[ $now == "$installed" ]] || CHANGED+=("$tool ${installed:--} -> ${now:--}")

    # Run whether or not the version moved, since an integration can be outdated against a herdr that was already current.
    if [[ $tool == "herdr" ]]; then
        herdr_post || FAILED+=("herdr integrations")
        if [[ $DRY_RUN == false ]] && herdr_restart_pending; then
            log "herdr: a running server is on the previous binary and is left running, restart it when no pane needs it"
        fi
    fi
}

install_tool() {
    local tool="$1" path now
    path=$(tool_path "$tool")
    if [[ -n $path ]]; then
        log "$tool: already installed at $(hide_home "$path"), leaving it alone, --upgrade updates it"
        return 0
    fi
    log "$tool: installing from $("$(tool_function "$tool" source)")"
    if ! "$(tool_function "$tool" install)"; then
        warn "$tool: its installer failed"
        FAILED+=("$tool")
        return 0
    fi
    [[ $DRY_RUN == true ]] && return 0
    extend_path
    path=$(tool_path "$tool")
    if [[ -z $path ]]; then
        warn "$tool: its installer finished, but no $tool is on PATH"
        FAILED+=("$tool")
        return 0
    fi
    now=$(tool_version "$path")
    CHANGED+=("$tool - -> ${now:--}")
}

apply() {
    log "Selected: ${SELECTED[*]}"
    if [[ $DRY_RUN == true ]]; then
        log "Mode: $MODE (dry run)"
    else
        log "Mode: $MODE"
    fi
    log ""
    confirm "Change the agent tools of $(id -un)?" || die "Declined"
    log ""

    local tool
    for tool in "${SELECTED[@]}"; do
        "${MODE}_tool" "$tool"
    done

    log ""
    if [[ ${#CHANGED[@]} -gt 0 ]]; then
        log "Changed:"
        local entry
        for entry in "${CHANGED[@]}"; do
            info "$entry"
        done
        info "A session already running keeps its version until it is restarted"
    else
        log "Nothing changed"
    fi

    if [[ ${#FAILED[@]} -gt 0 ]]; then
        log ""
        warn "Failed: ${FAILED[*]}"
        return 1
    fi
}

list_tools() {
    log "Managed agent tools:"
    local tool
    for tool in "${TOOLS[@]}"; do
        printf '  %-10s %s\n' "$tool" "$("$(tool_function "$tool" source)")"
    done
}

# --- Entry ---

resolve_selection() {
    local tool requested known
    for requested in "${REQUESTED[@]}"; do
        known=false
        for tool in "${TOOLS[@]}"; do
            [[ $tool == "$requested" ]] && known=true
        done
        [[ $known == true ]] || die "Unknown tool \"$requested\", --list names the managed tools"
    done

    if [[ ${#REQUESTED[@]} -eq 0 ]]; then
        if [[ $MODE == "install" && $INSTALL_ALL == false ]]; then
            die "--install names the tools it installs, or takes --all, so nothing is installed by default"
        fi
        SELECTED=("${TOOLS[@]}")
        return 0
    fi
    for tool in "${TOOLS[@]}"; do
        for requested in "${REQUESTED[@]}"; do
            [[ $tool == "$requested" ]] && SELECTED+=("$tool")
        done
    done
    return 0
}

parse_args() {
    local -a actions=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
        -r | --report) actions+=(report) ;;
        -u | --upgrade) actions+=(upgrade) ;;
        -i | --install) actions+=(install) ;;
        -l | --list) actions+=(list) ;;
        -a | --all) INSTALL_ALL=true ;;
        -j | --json) JSON_OUTPUT=true ;;
        -n | --dry-run) DRY_RUN=true ;;
        -y | --yes) ASSUME_YES=true ;;
        -h | --help)
            usage
            exit 0
            ;;
        -*) die "Unknown option \"$1\", --help lists the options" ;;
        *) REQUESTED+=("$1") ;;
        esac
        shift
    done

    # The order the actions were given is not a contract, so more than one is a refusal rather than the last one winning.
    if ((${#actions[@]} > 1)); then
        die "More than one action given (${actions[*]}), name one"
    fi
    [[ ${#actions[@]} -eq 1 ]] && MODE="${actions[0]}"

    if [[ $JSON_OUTPUT == true && $MODE != "report" ]]; then
        die "--json changes how a report is written, so it applies only to --report, and the $MODE action was given"
    fi
    if [[ $INSTALL_ALL == true && $MODE != "install" ]]; then
        die "--all names every tool for --install, and the $MODE action was given"
    fi
    if [[ $INSTALL_ALL == true && ${#REQUESTED[@]} -gt 0 ]]; then
        die "--all and a tool name were both given, name one or the other"
    fi
    return 0
}

main() {
    parse_args "$@"
    resolve_selection

    if [[ $MODE == "list" ]]; then
        list_tools
        exit 0
    fi

    # Root's home holds none of these tools on a host where a person uses them, so a root run would report everything absent and install into the wrong home.
    [[ $EUID -ne 0 ]] ||
        die "Agent tools live in a user's home, so this runs as that user, for example: runuser -u <user> -- $0 $*"
    [[ -n ${HOME:-} && -d $HOME ]] || die "HOME is not set to a directory, so there is no user's home to read the tools from"
    command -v timeout >/dev/null || die "timeout is required, install coreutils with this host's package manager"
    extend_path

    case "$MODE" in
    report) report ;;
    install | upgrade) apply ;;
    esac
}

main "$@"
