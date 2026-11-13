#!/bin/bash

# Keeps the coding-agent tools a user installed current, and installs one only when it is named.
# These tools live in the user's own home and each carries its own updater, so this script runs as that user, never as root.
# For the same reason it drives each tool's updater rather than replacing it.
# An upgrade touches only a tool already present, so a host where nobody installed codex never gets codex from a nightly run.
#
# A tool installed through a package manager is never given its own updater, which would fight the manager for the same files.
# An upgrade runs that manager's own upgrade for that one package instead: npm, bun, Homebrew, and mise.
# Nix is declarative and apt is upgraded by upgrade-host.sh, so each is reported and nothing is run.
# Nothing here elevates, so an npm prefix this user cannot write to is reported with its command and left.
# The package name is a per-tool record in this file (npm_package), because a manager's files do not always carry it.
# An npm or bun package must own the tool's files, so a fork providing the same command is reported and not replaced.
# A herdr upgraded by a manager gets the same integration and restart step the self-updater path runs.
# A mise shim is followed through mise which to the file it stands for.
# A Node runtime is never upgraded for an agent inside it, since the new Node would not hold the agent.
# A tool recording a managed copy (opencode) has other copies consolidated into it before any of that applies.
# An npm, bun, or Homebrew copy ahead of it is removed where that needs no root.
# The Homebrew name is read from where brew put the files, and the mise key from mise's own listing.
# The npm on PATH must confirm it owns the files, by their sitting under the global root it reports.
# Every step is idempotent, and a tool that fails is reported while the rest still run.

set -Eeuo pipefail

# The managed agent tools, in no particular order, since none depends on another.
readonly TOOLS=(claude codex opencode herdr cswap)

# Seconds one updater or installer may run before it is treated as hung.
# The limit is long enough for a slow download and short enough that a nightly run ends.
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

json_string() (
    export LC_ALL=C
    local s="$1" out="" i n b c cp need min j hi lo
    if [[ $s != *[!\ -~]* && $s != *[\"\\]* ]]; then
        printf '"%s"' "$s"
        return 0
    fi
    n=${#s}
    for ((i = 0; i < n; i++)); do
        printf -v b '%d' "'${s:i:1}"
        b=$((b & 0xff))
        if ((b == 34 || b == 92)); then
            out+="\\${s:i:1}"
        elif ((b < 32)); then
            printf -v c '\\u%04x' "$b"
            out+=$c
        elif ((b < 128)); then
            out+=${s:i:1}
        else
            if ((b >= 0xc2 && b <= 0xdf)); then
                need=1 cp=$((b & 0x1f)) min=0x80
            elif ((b >= 0xe0 && b <= 0xef)); then
                need=2 cp=$((b & 0x0f)) min=0x800
            elif ((b >= 0xf0 && b <= 0xf4)); then
                need=3 cp=$((b & 0x07)) min=0x10000
            else
                continue
            fi
            for ((j = 1; j <= need; j++)); do
                ((i + j < n)) || break
                printf -v c '%d' "'${s:i+j:1}"
                c=$((c & 0xff))
                ((c >= 0x80 && c <= 0xbf)) || break
                cp=$(((cp << 6) | (c & 0x3f)))
            done
            if ((j <= need || cp < min || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))); then
                continue
            fi
            i=$((i + need))
            if ((cp < 0x10000)); then
                printf -v c '\\u%04x' "$cp"
            else
                cp=$((cp - 0x10000))
                hi=$((0xd800 + (cp >> 10)))
                lo=$((0xdc00 + (cp & 0x3ff)))
                printf -v c '\\u%04x\\u%04x' "$hi" "$lo"
            fi
            out+=$c
        fi
    done
    printf '"%s"' "$out"
)

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
  -u, --upgrade     Upgrade each tool already installed, and install only a managed copy
  -i, --install     Install the named tools that are missing, leave an installed one alone,
                    except a copy consolidation replaces with the managed one
  -l, --list        List the managed tools and where each installs from
  -h, --help        Show this help

Options:
  -a, --all         With --install, name every managed tool
  -j, --json        Write the report as JSON rather than a table, for a program to read
  -n, --dry-run     Print the commands instead of running them
  -y, --yes         Do not prompt before changing anything

With no tool named, --report and --upgrade select every managed tool, while --install selects
none, so an install always names what it installs, or says --all.

A tool reads as "self" when no package manager owns it, so this script runs the tool's own
updater, and as the name of a package manager when one owns it. --upgrade then runs that
manager's upgrade for that one package: npm or bun install the latest tag, Homebrew upgrades the
formula or cask, and mise upgrades the tool. apt is upgraded by upgrade-host.sh, and Nix is
declarative, so neither is run. An npm directory this user cannot write is reported with its
command rather than run, and nothing here ever uses sudo. herdr's agent integrations are reinstalled after an upgrade wherever herdr reports
one outdated, and a herdr server still running the previous binary is left running and reported
as needing a restart, since stopping it would end every pane inside it.

opencode is the one tool with a managed copy, ~/.opencode/bin/opencode from its own installer,
since it ships from several sources and opencode 2 replaced opencode 1's npm package. Any other
copy PATH reaches first, an npm opencode-ai among them, is consolidated: --upgrade removes it
through npm, bun, or Homebrew after a prompt and installs the managed copy, and --install does the
same where no managed copy is in place yet. Nothing is removed while the managed copy cannot be
installed, or while any copy ahead of it is one only its owner can remove. Such a copy, or one
whose removal is declined, stays and is handled as a tool without a managed copy is, and the
report says what owns it. Running the installer again is how the managed copy is upgraded.

--json writes one object: "schema" (1), "platform" ("linux"), "tools", one entry per tool with
"tool", "installed", "path", "manager", "status" ("present" or "absent"), "restart_pending" (true,
false, or null where the tool cannot say) and that tool's own "notes", and a top-level "notes".

It exits 0 on success, 3 where a tool failed or was refused while the others still ran, and 1 where the run itself refused or stopped.

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

# Fetches an installer to a file and runs it from there rather than piping it into a shell.
# A truncated download then fails before it runs rather than part way through.
run_installer() {
    local url="$1" shell="$2" file
    file="$TMP_DIR/$(basename "$url").installer"
    if [[ $DRY_RUN == true ]]; then
        printf '  [dry run] curl -fsSL %s -o %s && %s %s\n' "$url" "$file" "$shell" "$file"
        return 0
    fi
    command -v curl >/dev/null || {
        warn "curl is not installed, so $url cannot be fetched"
        return 1
    }
    curl -fsSL --proto '=https' --tlsv1.2 --retry 2 --connect-timeout 15 --max-time "$STEP_TIMEOUT" --retry-max-time "$STEP_TIMEOUT" "$url" -o "$file" || return 1
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

# The npm registry name of a tool, which npm and bun both install from, or nothing where none is recorded.
# These are the names each tool's own documentation gives, and herdr and cswap are not published there.
npm_package() {
    case "$1" in
    claude) printf '@anthropic-ai/claude-code' ;;
    codex) printf '@openai/codex' ;;
    opencode) printf '@opencode/cli' ;;
    esac
}

# Every npm registry name a tool has shipped under, current first, one per line.
# A copy under an earlier name is still that tool's, so consolidation can remove it, as it does opencode 1's opencode-ai.
npm_packages() {
    npm_package "$1"
    printf '\n'
    case "$1" in
    opencode) printf 'opencode-ai\n' ;;
    esac
}

# The global root of the npm on PATH, real-pathed, and empty where npm cannot say.
# Every caller runs this inside $(...), so nothing it set would outlive the call, and it caches nothing.
npm_root() {
    local root=""
    if command -v npm >/dev/null; then
        root=$(timeout 30 npm root -g 2>/dev/null </dev/null) || root=""
        [[ -z $root ]] || root=$(readlink -f -- "$root" 2>/dev/null) || root=""
    fi
    printf '%s' "$root"
}

# What owns the installed copy, read from where the command resolves to rather than from how it was meant to be installed.
# The real path of a tool, with a mise shim followed to the file it stands for.
# The shims of mise are symlinks to the mise executable, so a tool whose real path is named mise is asked of mise itself.
# Returns 1 and prints nothing where mise cannot say, so a shim is never read as the manager that holds mise.
tool_real_path() {
    local path="$1" tool real out
    tool="${path##*/}"
    real=$(readlink -f -- "$path" 2>/dev/null) || real="$path"
    if [[ ${real##*/} == "mise" && $tool != "mise" ]]; then
        out=$(timeout 30 mise which "$tool" 2>/dev/null </dev/null) || return 1
        [[ -n $out ]] || return 1
        real=$(readlink -f -- "$out" 2>/dev/null) || real="$out"
    fi
    printf '%s' "$real"
}

tool_manager() {
    local path="$1" real
    # A mise shim that mise cannot resolve is still mise's, and its plan reports that nothing runs.
    real=$(tool_real_path "$path") || {
        printf 'mise'
        return 0
    }
    # The npm on PATH is asked first, since an ordinary npm global can sit under another manager's node or bun directory.
    # Upgrading that node would leave the tool behind, so what npm holds is npm's whatever else the path looks like.
    local root
    root=$(npm_root)
    if [[ -n $root && $real == "$root"/* ]]; then
        printf 'npm'
        return 0
    fi
    tool_manager_by_path "$real"
}

# Bun's global package directory, real-pathed where it exists, so one expression serves classification and the plan.
# BUN_INSTALL and BUN_INSTALL_GLOBAL_DIR are honored, and the install.globalDir in bunfig.toml is not read.
bun_global_dir() {
    local dir="${BUN_INSTALL_GLOBAL_DIR:-${BUN_INSTALL:-$HOME/.bun}/install/global}" real
    real=$(readlink -f -- "$dir" 2>/dev/null) || real="$dir"
    printf '%s' "$real"
}

# The manager a path suggests once npm has declined it.
tool_manager_by_path() {
    local real="$1" bun_global
    # A relocated bun carries no .bun segment, so bun's own variables name its global directory.
    bun_global=$(bun_global_dir)
    if [[ -n $bun_global && $real == "$bun_global"/* ]]; then
        printf 'bun'
        return 0
    fi
    # MISE_DATA_DIR can move mise's installs off any /mise/installs/ path, so mise's own listing is asked before any shape.
    # Read by shape there, a relocated mise tool would fall to its own updater over mise's files.
    if [[ -n $(mise_key "$real") ]]; then
        printf 'mise'
        return 0
    fi
    case "$real" in
    # A mise, Homebrew, bun, or Nix directory can hold a node_modules of its own, so each is read before the generic npm shape.
    */mise/installs/*) printf 'mise' ;;
    */Cellar/* | */Caskroom/*) printf 'brew' ;;
    */.bun/*) printf 'bun' ;;
    /nix/store/*) printf 'nix' ;;
    */node_modules/*) printf 'npm' ;;
    */linuxbrew/* | /opt/homebrew/*) printf 'brew' ;;
    *)
        if [[ $real == /usr/* && $real != /usr/local/* ]] && command -v dpkg >/dev/null && dpkg -S "$real" >/dev/null 2>&1; then
            printf 'apt'
        else
            printf 'self'
        fi
        ;;
    esac
}

# The key mise lists the tool under, such as npm:opencode-ai, for the entry whose install path holds the given real path.
# Prints nothing where mise, jq, or a matching entry is missing, so nothing is upgraded on a guess.
mise_key() {
    local real="$1" out key install
    command -v mise >/dev/null && command -v jq >/dev/null || return 0
    out=$(timeout 30 mise ls --json 2>/dev/null </dev/null) || return 0
    while IFS=$'\t' read -r key install; do
        [[ -n $install ]] || continue
        install=$(readlink -f -- "$install" 2>/dev/null) || continue
        if [[ $real == "$install"/* ]]; then
            printf '%s' "$key"
            return 0
        fi
    done < <(jq -r 'to_entries[] | .key as $k | .value[]? | select(.install_path != null) | [$k, .install_path] | @tsv' <<<"$out" 2>/dev/null)
    return 0
}

# The npm or bun command for a step, upgrade or remove, on the package owning a real path, as PKG_CMD, or PKG_WHY where none runs.
# An upgrade and a consolidation share it, so a copy under a name the tool shipped under earlier is upgraded or removed by that name.
# One of the tool's recorded names must own the files, so a fork providing the same command is never replaced or removed.
PKG_CMD=()
PKG_WHY=""

package_step() {
    local step="$1" tool="$2" real="$3" manager="$4" recorded pkg="" named candidate dir action
    local -a command=()
    PKG_CMD=()
    PKG_WHY=""
    if [[ $step == "upgrade" ]]; then action="upgrade"; else action="remove"; fi
    recorded=$(npm_package "$tool")
    if [[ -z $recorded ]]; then
        PKG_WHY="no $manager package name is recorded for $tool, so $action it with $manager by hand"
        return 0
    fi
    # The path shape only suggests npm, so the npm on PATH is asked where it installs and must own the files.
    if [[ $manager == "bun" ]]; then dir="$(bun_global_dir)/node_modules"; else dir=$(npm_root); fi
    while IFS= read -r candidate; do
        if [[ -n $candidate && -n $dir && $real == "$dir/$candidate"/* ]]; then
            pkg="$candidate"
            break
        fi
    done < <(npm_packages "$tool")
    # Where npm or bun cannot say which package owns the files, the command names the one the path itself shows.
    named="$pkg"
    if [[ -z $named ]]; then
        while IFS= read -r candidate; do
            if [[ -n $candidate && $real == */node_modules/"$candidate"/* ]]; then
                named="$candidate"
                break
            fi
        done < <(npm_packages "$tool")
    fi
    if [[ $step == "upgrade" ]]; then
        # Installing the latest tag rather than npm update, which keeps a global package inside the range it was installed at.
        command=("$manager" install -g "${named:-$recorded}@latest")
    elif [[ $manager == "bun" ]]; then
        command=(bun remove -g "${named:-$recorded}")
    else
        command=(npm uninstall -g "${named:-$recorded}")
    fi
    if ! command -v "$manager" >/dev/null; then
        PKG_WHY="$manager is not on PATH, so run: ${command[*]}"
    elif [[ $manager == "bun" && -z $pkg ]]; then
        PKG_WHY="its files at $(hide_home "$real") are not under bun's global package $recorded, so that package does not own it, $action it where it was installed"
    elif [[ -z $dir || $real != "$dir"/* ]]; then
        PKG_WHY="its files at $(hide_home "$real") are not under the global root of the npm on PATH, so that npm does not own it, $action it where it was installed"
    elif [[ -z $pkg ]]; then
        PKG_WHY="its files at $(hide_home "$real") are not under the npm package $recorded, so that package does not own it, $action it where it was installed"
    elif [[ $manager == "npm" && ! -w $dir ]]; then
        PKG_WHY="the npm global directory $(hide_home "$dir") is not writable by this user, so run as its owner: ${command[*]}"
    else
        PKG_CMD=("${command[@]}")
    fi
}

# The brew command for a verb, upgrade or uninstall, on the package holding a real path, as BREW_CMD, or BREW_WHY where none runs.
# An upgrade and a consolidation share it, so both read the name from where brew put the files and both leave Homebrew's Node alone.
BREW_CMD=()
BREW_WHY=""

brew_step() {
    local verb="$1" tool="$2" real="$3" name
    BREW_CMD=()
    BREW_WHY=""
    if [[ $real == */Caskroom/* ]]; then
        name="${real#*/Caskroom/}"
        BREW_CMD=(brew "$verb" --cask "${name%%/*}")
    elif [[ $real == */Cellar/* ]]; then
        name="${real#*/Cellar/}"
        name="${name%%/*}"
        if [[ $name == "node" || $name == node@* ]]; then
            BREW_WHY="it sits inside Homebrew's Node, and the npm on PATH does not own it, so $verb it where it was installed"
            return 0
        fi
        BREW_CMD=(brew "$verb" "$name")
    else
        BREW_WHY="Homebrew's name for $tool could not be read from $(hide_home "$real"), so run brew $verb with it by hand"
        return 0
    fi
    command -v brew >/dev/null || {
        BREW_WHY="brew is not on PATH, so run: ${BREW_CMD[*]}"
        BREW_CMD=()
    }
}

# What an upgrade does for a tool a package manager owns, as PLAN_CMD (the command to run) or PLAN_WHY (why none runs).
# Never both.
# The reason names the command where one exists, so the person can run it where this script may not.
PLAN_CMD=()
PLAN_WHY=""

manager_plan() {
    local tool="$1" path="$2" manager="$3" real pkg dir name
    PLAN_CMD=()
    PLAN_WHY=""
    if ! real=$(tool_real_path "$path"); then
        PLAN_WHY="$tool is a mise shim and mise which could not resolve it, so upgrade it with mise by hand"
        return 0
    fi
    case "$manager" in
    apt)
        PLAN_WHY="apt owns it and upgrade-host.sh upgrades it"
        ;;
    nix)
        PLAN_WHY="Nix is declarative, so update it where it is declared, for example with nix profile upgrade or in your flake or home configuration"
        ;;
    npm | bun)
        package_step upgrade "$tool" "$real" "$manager"
        PLAN_CMD=("${PKG_CMD[@]}")
        PLAN_WHY="$PKG_WHY"
        ;;
    brew)
        brew_step upgrade "$tool" "$real"
        PLAN_CMD=("${BREW_CMD[@]}")
        PLAN_WHY="$BREW_WHY"
        ;;
    mise)
        # The tool is named by mise's own listing, matched on install path, rather than read from the directory name.
        name=$(mise_key "$real")
        if [[ -z $name ]]; then
            PLAN_WHY="mise and jq must both be on PATH and mise's listing must hold an entry whose install path holds $(hide_home "$real"), and none does, so upgrade it with mise by hand"
            return 0
        fi
        # A Node runtime is never upgraded for an agent in it, which would leave the agent behind in the old Node.
        case "$name" in
        node | core:node | nodejs)
            PLAN_WHY="it sits inside mise's Node, and the npm on PATH does not own it, so upgrade it where it was installed"
            return 0
            ;;
        esac
        PLAN_CMD=(mise upgrade "$name")
        ;;
    esac
}

# --- Consolidation ---

# A tool can record one managed copy, the one this script installs and upgrades, as install-tools.sh does for its own tools.
# Any other copy PATH resolves to ahead of it is consolidated: removed through whatever owns it, so the managed copy is what PATH reaches.
# A tool recording none keeps whatever copy it has and is upgraded through whatever owns it.
tool_managed() {
    if declare -F "$(tool_function "$1" managed)" >/dev/null; then
        "$(tool_function "$1" managed)"
    fi
    return 0
}

# Whether a path is the tool's managed copy, compared by identity so a link to it counts.
is_managed_copy() {
    local tool="$1" path="$2" managed real
    managed=$(tool_managed "$tool")
    [[ -n $managed && -e $managed ]] || return 1
    real=$(tool_real_path "$path") || return 1
    [[ $real -ef $managed ]]
}

# Whether a copy of the tool PATH resolves to is one consolidation removes.
is_stray_copy() {
    local tool="$1" path="$2"
    [[ -n $(tool_managed "$tool") ]] || return 1
    ! is_managed_copy "$tool" "$path"
}

# What removing a stray copy runs, as STRAY_CMD (the command) or STRAY_WHY (why none runs, naming the command where one exists).
# Never both.
# Only a removal this user can run without elevating is run, so a system package, Nix, or mise's declared tool is named rather than touched.
STRAY_CMD=()
STRAY_WHY=""

stray_plan() {
    local tool="$1" path="$2" manager="$3" real pkg dir name
    STRAY_CMD=()
    STRAY_WHY=""
    if ! real=$(tool_real_path "$path"); then
        STRAY_WHY="it is a mise shim and mise which could not resolve it, so remove it with mise by hand"
        return 0
    fi
    case "$manager" in
    npm | bun)
        package_step remove "$tool" "$real" "$manager"
        STRAY_CMD=("${PKG_CMD[@]}")
        STRAY_WHY="$PKG_WHY"
        ;;
    brew)
        brew_step uninstall "$tool" "$real"
        STRAY_CMD=("${BREW_CMD[@]}")
        STRAY_WHY="$BREW_WHY"
        ;;
    apt)
        name=$(dpkg -S "$real" 2>/dev/null) || name=""
        name="${name%%:*}"
        STRAY_WHY="apt owns it${name:+ as the package $name}, and removing it needs root, so run as root: apt remove ${name:-<package>}"
        ;;
    mise)
        name=$(mise_key "$real")
        STRAY_WHY="mise declares it, so remove ${name:-it} from mise's configuration, for example with: mise unuse -g ${name:-<tool>}"
        ;;
    nix)
        STRAY_WHY="Nix is declarative, so remove it where it is declared, for example with nix profile remove or in your flake or home configuration"
        ;;
    self)
        STRAY_WHY="no package manager owns it, so delete it by hand: rm $(hide_home "$path")"
        ;;
    *)
        STRAY_WHY="$manager owns it, so remove it with $manager by hand"
        ;;
    esac
}

# Every copy of a tool PATH holds ahead of its managed copy, nearest first, one per line.
stray_copies() {
    local tool="$1" path
    while IFS= read -r path; do
        [[ -n $path ]] || continue
        is_managed_copy "$tool" "$path" && return 0
        printf '%s\n' "$path"
    done < <(type -ap "$tool" 2>/dev/null)
    return 0
}

# Why a copy ahead of the managed one cannot be removed, or nothing where every one can.
# Read before any copy is removed, so a removal never leaves an older copy this script cannot touch answering instead.
stray_blocker() {
    local tool="$1" path
    while IFS= read -r path; do
        stray_plan "$tool" "$path" "$(tool_manager "$path")"
        if ((${#STRAY_CMD[@]} == 0)); then
            printf '%s stays, since %s' "$(hide_home "$path")" "$STRAY_WHY"
            return 0
        fi
    done < <(stray_copies "$tool")
    return 0
}

# Why the managed copy cannot be installed on this host, or nothing where it can.
# Read before any copy is removed, so consolidation never leaves a host with no copy at all.
managed_install_refusal() {
    command -v curl >/dev/null || printf 'curl is not installed, so its installer cannot be fetched'
    return 0
}

# Removes each stray copy PATH resolves to, nearest first, until PATH reaches the managed copy or nothing.
# A loop, since PATH can stack more than one copy ahead of the managed one.
# Returns 0 once no stray copy is reached, and 1 where a removal failed.
# Returns 2 where a copy stays, because this script cannot remove it or the prompt was declined.
consolidate() {
    local tool="$1" path manager n
    for ((n = 0; n < 8; n++)); do
        path=$(tool_path "$tool")
        [[ -n $path ]] || return 0
        is_stray_copy "$tool" "$path" || return 0
        manager=$(tool_manager "$path")
        stray_plan "$tool" "$path" "$manager"
        if ((${#STRAY_CMD[@]} == 0)); then
            log "$tool: $(hide_home "$path") is the $manager copy rather than the managed $(hide_home "$(tool_managed "$tool")"), and it stays: $STRAY_WHY"
            return 2
        fi
        log "$tool: $(hide_home "$path") is the $manager copy rather than the managed $(hide_home "$(tool_managed "$tool")"), removing it"
        if ! confirm "  Run ${STRAY_CMD[*]}?"; then
            log "$tool: left $(hide_home "$path") in place"
            return 2
        fi
        if ! run "${STRAY_CMD[@]}"; then
            warn "$tool: ${STRAY_CMD[*]} failed"
            return 1
        fi
        # Under --dry-run nothing is removed, so the same copy would resolve again forever.
        [[ $DRY_RUN == true ]] && return 0
        CHANGED+=("$tool: removed the $manager copy at $(hide_home "$path")")
        hash -r
    done
    warn "$tool: a copy still resolves ahead of the managed one after $n removals"
    return 1
}

# Consolidates a tool, then installs its managed copy where none is in place.
# Shared by --install and --upgrade, so both leave the same single copy behind.
# Returns 0 where a managed copy is in place for the caller to upgrade, and 1 where this call finished the tool.
# Returns 2 where a stray copy stays, so the caller handles that copy as it would any tool without a managed copy.
consolidate_and_install() {
    local tool="$1" installed="$2" managed refusal blocker rc=0
    managed=$(tool_managed "$tool")
    if [[ ! -x $managed ]]; then
        refusal=$(managed_install_refusal "$tool")
        if [[ -n $refusal ]]; then
            log "$tool: the copy PATH reaches stays, since the managed copy cannot be installed: $refusal"
            return 2
        fi
    fi
    blocker=$(stray_blocker "$tool")
    if [[ -n $blocker ]]; then
        log "$tool: nothing is removed, since $blocker"
        return 2
    fi
    consolidate "$tool" || rc=$?
    if ((rc == 1)); then
        FAILED+=("$tool")
        return 1
    fi
    ((rc == 0)) || return 2
    [[ -x $managed ]] && return 0
    log "$tool: installing the managed copy from $("$(tool_function "$tool" source)")"
    if ! "$(tool_function "$tool" install)"; then
        warn "$tool: its installer failed after the copy ahead of it was removed, so run --install $tool to install it again"
        FAILED+=("$tool")
        return 1
    fi
    [[ $DRY_RUN == true ]] && return 1
    if [[ ! -x $managed ]]; then
        warn "$tool: its installer finished, but no managed copy is at $(hide_home "$managed")"
        FAILED+=("$tool")
        return 1
    fi
    extend_path
    hash -r
    record_change "$tool" "$installed"
    return 1
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

# The opencode tool ships from its own installer, npm, bun, Homebrew, and mise, so it records the one copy every other is consolidated into.
# The installer writes ~/.opencode/bin, and running it again is the documented update, which also carries an opencode 1 there to opencode 2.
opencode_source() { printf 'https://opencode.ai/v2/install'; }
opencode_managed() { printf '%s' "$HOME/.opencode/bin/opencode"; }
# A copy elsewhere that consolidation could not remove keeps its own updater, as it had before opencode recorded a managed copy.
opencode_upgrade() {
    if is_managed_copy opencode "$1"; then
        run_installer "$(opencode_source)" bash
    else
        run "$1" upgrade
    fi
}
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

# Read from herdr's own status rather than from whether a session is open.
# This is because a session on the current binary needs no restart however many panes it holds.
# Returns 0 when a restart is pending, 1 when none is, and 2 when herdr's answer could not be read.
herdr_restart_pending() {
    local out answer
    command -v jq >/dev/null || return 2
    out=$(timeout 30 herdr status --json 2>/dev/null </dev/null) || return 2
    answer=$(jq -r '.update.restart_needed | if type == "boolean" then tostring else "unknown" end' <<<"$out" 2>/dev/null) || return 2
    case "$answer" in
    true) return 0 ;;
    false) return 1 ;;
    *) return 2 ;;
    esac
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
    local rc=0
    herdr_restart_pending || rc=$?
    case $rc in
    0) note herdr "a running server is on the previous binary, restart it when no pane needs it: herdr server stop" ;;
    2) note herdr "could not read whether a running server needs a restart" ;;
    esac
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
            local upgrades=true removes=false refusal="" blocker=""
            if is_stray_copy "$tool" "$path"; then
                [[ -x $(tool_managed "$tool") ]] || refusal=$(managed_install_refusal "$tool")
                blocker=$(stray_blocker "$tool")
                stray_plan "$tool" "$path" "$manager"
                if ((${#STRAY_CMD[@]} > 0)) && [[ -n $blocker ]]; then
                    note "$tool" "$(hide_home "$path") is the $manager copy rather than the managed $(hide_home "$(tool_managed "$tool")"), and it stays, since $blocker"
                elif ((${#STRAY_CMD[@]} > 0)) && [[ -z $refusal ]]; then
                    note "$tool" "$(hide_home "$path") is the $manager copy rather than the managed $(hide_home "$(tool_managed "$tool")"), --upgrade removes it with ${STRAY_CMD[*]}, then $([[ -x $(tool_managed "$tool") ]] && printf upgrades || printf installs) the managed one"
                    removes=true
                else
                    note "$tool" "$(hide_home "$path") is the $manager copy rather than the managed $(hide_home "$(tool_managed "$tool")"), and it stays: ${refusal:-$STRAY_WHY}"
                fi
            fi
            if [[ $removes == false && $manager != "self" ]]; then
                manager_plan "$tool" "$path" "$manager"
                if ((${#PLAN_CMD[@]} > 0)); then
                    note "$tool" "installed through $manager, --upgrade runs: ${PLAN_CMD[*]}"
                else
                    note "$tool" "installed through $manager, --upgrade does not run it: $PLAN_WHY"
                    upgrades=false
                fi
            fi
            # The post step runs after the self-updater and after a manager upgrade alike, so the notes about it do too.
            # A manager that does not run leaves the post step unrun, so the notes would promise what --upgrade does not do.
            if [[ $tool == "herdr" && $upgrades == true ]]; then
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

record_change() {
    local tool="$1" installed="$2" path now=""
    path=$(tool_path "$tool")
    [[ -z $path ]] || now=$(tool_version "$path")
    [[ $now == "$installed" ]] || CHANGED+=("$tool ${installed:--} -> ${now:--}")
}

upgrade_tool() {
    local tool="$1" path installed manager
    path=$(tool_path "$tool")
    if [[ -z $path ]]; then
        log "$tool: not installed, leaving it absent"
        return 0
    fi
    if is_stray_copy "$tool" "$path"; then
        local rc=0
        consolidate_and_install "$tool" "$(tool_version "$path")" || rc=$?
        # A stray copy that stays is handled as a tool without a managed copy is.
        case $rc in
        0) path=$(tool_managed "$tool") ;;
        # An earlier pass may have removed a copy, so the one that stays is read again.
        2)
            path=$(tool_path "$tool")
            if [[ -z $path ]]; then
                log "$tool: not installed, leaving it absent"
                return 0
            fi
            ;;
        *) return 0 ;;
        esac
    fi
    manager=$(tool_manager "$path")
    installed=$(tool_version "$path")
    if [[ $manager != "self" ]]; then
        manager_upgrade "$tool" "$path" "$manager" "$installed"
        return 0
    fi

    # The herdr updater refuses to run from inside one of its own panes.
    # A run there therefore skips that one step rather than failing on a refusal it already knows is coming.
    if [[ $tool == "herdr" && -n ${HERDR_PANE_ID:-} ]]; then
        log "herdr: at ${installed:-an unreadable version}, and this run is inside a herdr pane, which herdr will not update from, run this outside herdr to update it"
        herdr_post || FAILED+=("herdr integrations")
        return 0
    fi

    log "$tool: at ${installed:-an unreadable version}, running its updater"
    if ! "$(tool_function "$tool" upgrade)" "$path"; then
        warn "$tool: its updater failed"
        FAILED+=("$tool")
        record_change "$tool" "$installed"
        return 0
    fi
    record_change "$tool" "$installed"

    [[ $tool != "herdr" ]] || herdr_after_upgrade
}

# Run whether or not the version moved, since an integration can be outdated against a herdr that was already current.
# One path for the self-updater and for a manager, so both reinstall the integrations and give the same restart guidance.
herdr_after_upgrade() {
    herdr_post || FAILED+=("herdr integrations")
    if [[ $DRY_RUN == false ]]; then
        local rc=0
        herdr_restart_pending || rc=$?
        case $rc in
        0) log "herdr: a running server is on the previous binary and is left running, restart it when no pane needs it" ;;
        2) log "herdr: could not read whether a running server needs a restart" ;;
        esac
    fi
}

# Upgrades a tool through the package manager that owns it, or says why nothing runs.
# A failing manager is recorded and the other tools still run, as a failing updater is.
manager_upgrade() {
    local tool="$1" path="$2" manager="$3" installed="$4"
    manager_plan "$tool" "$path" "$manager"
    if ((${#PLAN_CMD[@]} == 0)); then
        log "$tool: installed through $manager, not run here: $PLAN_WHY"
        return 0
    fi
    log "$tool: at ${installed:-an unreadable version}, installed through $manager, running: ${PLAN_CMD[*]}"
    if ! run "${PLAN_CMD[@]}"; then
        warn "$tool: $manager failed to upgrade it"
        FAILED+=("$tool")
        record_change "$tool" "$installed"
        return 0
    fi
    record_change "$tool" "$installed"
    [[ $tool != "herdr" ]] || herdr_after_upgrade
}

install_tool() {
    local tool="$1" path now
    path=$(tool_path "$tool")
    if [[ -n $path ]] && is_stray_copy "$tool" "$path"; then
        # As in install-tools.sh, --install with a managed copy in place leaves it at its version, so removing what resolves ahead of it waits for --upgrade.
        if [[ -x $(tool_managed "$tool") ]]; then
            log "$tool: $(hide_home "$path") resolves ahead of the managed copy, --upgrade removes it, --install leaves it to avoid downgrading what resolves ahead of it"
            return 0
        fi
        local rc=0
        consolidate_and_install "$tool" "$(tool_version "$path")" || rc=$?
        ((rc == 2)) || return 0
        # An earlier pass may have removed a copy, so the one that stays is read again.
        path=$(tool_path "$tool")
    fi
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
        return 3
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

    # Root's home holds none of these tools on a host where a person uses them.
    # A root run would therefore report everything absent and install into the wrong home.
    [[ $EUID -ne 0 ]] ||
        die "Agent tools live in a user's home, so this runs as that user, for example: runuser -u <user> -- install-agents.sh $*"
    [[ -n ${HOME:-} && -d $HOME ]] || die "HOME is not set to a directory, so there is no user's home to read the tools from"
    extend_path

    case "$MODE" in
    report) report ;;
    install | upgrade) apply ;;
    esac
}

main "$@"
