#!/bin/bash

# Meant to be sourced, never run: `. "$REPO/lib/fleet/sh/secrets.sh"` from a script that has already found its own tree through `BASH_SOURCE`.
# It sets no shell option, since sourcing it would change the caller's own.
# Every function answers through its output or its status under any shell option.
#
# The hub hosts this file and a repository carries it byte-for-byte, per GOVERNANCE.md "Hub-Hosted Tooling".
# So a repository extends it in a file of its own outside the carried directory that sources it, never by editing it.
# A caller that needs these functions in an elevated shell ships them there by name with `declare -f`.
# This is because sourcing this file in the calling shell does not reach one started by `sudo`.
# A function here that calls another is shipped with it, so `secret_verdict` needs `secret_shape_verdict`, `secret_state`, `unresolved_link_reason`, `secret_content_verdict`, `secret_content_state`, and `secret_content_refusal`.

# `unresolved_link_reason`
# Echoes the clause that says why a symlink answering `-L` true and `-e` false does not resolve, worded for the account running it.
# Echoing it lets every site that reports such a link compose one sentence instead of spelling its own.
# Root with its DAC override enters every directory.
# A link it cannot resolve therefore has a target that is missing or loops back on itself, and the clause says only that.
# A uid 0 without the override, such as one in a user namespace facing an unmapped owner, is read as root all the same.
# Any other account cannot tell those two from a target sitting behind a directory it cannot enter, and the clause names all three.
# A caller that tells the two wordings apart does so by the ending ` sits behind a directory this account cannot enter`, fixed text here.
unresolved_link_reason() {
    if [[ $EUID -eq 0 ]]; then
        echo "its target is missing or loops back on itself"
    else
        echo "its target is missing, loops back on itself, or sits behind a directory this account cannot enter"
    fi
}

# `secret_state` <path>
# One word for what a secret file is, because a gate that skips on a bare `-f` reads four unrelated states as one.
# The gate does so because `-f` answers false for each of them.
# `absent` only where nothing is at the path and every directory leading to it is one this account can actually enter.
# It is the single state a gate may skip on.
# `damaged <sentence>` where the path or a directory leading to it is a symlink that does not resolve.
# `damaged <sentence>` also where the path or a directory leading to it is present in a shape a secret cannot be read from.
# `unknown <sentence>` where a directory leading to it cannot be entered, or the path is not absolute.
# In those cases the path's absence was never established rather than observed.
# `present` where a regular file is there.
# The walk starts at the root, not the immediate parent, since a single unenterable ancestor makes every component below it answer false to `-e`.
# That answer would otherwise read as a proven absence.
# See `secret_verdict` below for the full check built on it.
secret_state() {
    local path="$1" walked="" part index last
    if [[ $path != /* ]]; then
        echo "unknown $path is not an absolute path"
        return 0
    fi
    # A doubled or trailing slash names the same component again.
    # Both are therefore removed before the split rather than becoming empty components the walk would have to skip.
    while [[ $path == *//* ]]; do
        path="${path//\/\//\/}"
    done
    path="${path%/}"
    # The trim above empties a path that was only slashes.
    # Such a path would otherwise walk no components at all and fall through to `present` without ever reaching the leaf test.
    # The root is a directory, so it gets the same answer any other non-regular-file leaf gets rather than a fifth state.
    if [[ -z $path ]]; then
        echo "damaged / is not a regular file"
        return 0
    fi
    local -a parts
    # Split on `/` alone, since `read` stops at the first newline and a path component may hold one.
    # A split that fails answers rather than walking no components.
    readarray -d / -t parts < <(printf '%s' "${path#/}") || {
        echo "unknown $path could not be split into its components"
        return 0
    }
    last=$((${#parts[@]} - 1))
    for index in "${!parts[@]}"; do
        part="${parts[index]}"
        walked="$walked/$part"
        # `-L` before `-e` throughout: a symlink that does not resolve answers false to `-e`.
        # That is exactly the state a bare existence test reads as an absence.
        # Reported as unresolved rather than as a missing target.
        # The reason is that `-e` cannot separate a target that is gone from one that loops back on itself.
        # For a non-root account, it cannot separate a target that is gone from one sitting behind a directory this account cannot enter.
        # Asserting any one of them would also claim more than the test established.
        if [[ -L $walked && ! -e $walked ]]; then
            echo "damaged $walked is a symlink that does not resolve, so $(unresolved_link_reason)"
            return 0
        fi
        if [[ ! -e $walked ]]; then
            echo absent
            return 0
        fi
        if [[ $index -eq $last ]]; then
            if [[ ! -f $walked ]]; then
                echo "damaged $walked is not a regular file"
                return 0
            fi
            echo present
            return 0
        fi
        if [[ ! -d $walked ]]; then
            echo "damaged $walked is not a directory, so it can hold no secret"
            return 0
        fi
        if [[ ! -x $walked ]]; then
            echo "unknown $walked cannot be entered by this account"
            return 0
        fi
    done
    echo present
}

# `secret_shape_verdict` <path> [private|shared]
# What `secret_verdict` below answers, from the file's metadata alone, without opening it.
# `readable` where it is a regular file, not a symlink, gives group and other no access, and this account can read it.
# `shared` drops the group and other check alone, for a secret a container reads as a Docker secret.
# Such a secret stays group and other readable by design.
# `absent` where nothing is there, which `secret_state` above establishes rather than infers, so a caller whose secret is optional may skip on it.
# `refused <sentence>` for everything else, the sentence naming the path and why, so a caller prints it as the reason rather than composing its own.
# A symlink is refused by name even where it resolves to a private file.
# One reason is that `stat -c %a` reads the link's own 777 rather than its target's mode.
# Another is that a link is also a second place the file's content can be repointed from.
# A caller running as root can call this one alone and run `secret_content_state` in the shell it reads the file with.
# That way root never opens the file.
secret_shape_verdict() {
    local path="$1" access="${2:-private}" state mode
    # Refused rather than normalized, since the caller reads the spelling it was given, and the kernel answers a trailing slash on a file with "Not a directory".
    if [[ $path == */ ]]; then
        printf 'refused %s ends in a slash, so it names a directory rather than a file\n' "$path"
        return 0
    fi
    state=$(secret_state "$path")
    case $state in
    present) ;;
    absent)
        echo absent
        return 0
        ;;
    *)
        printf 'refused %s\n' "${state#* }"
        return 0
        ;;
    esac
    if [[ -L $path ]]; then
        printf 'refused %s is a symlink, and must be the file itself\n' "$path"
        return 0
    fi
    # Validated as octal digits before `(( ))` reads it, since a non-octal value there fails the test the same way a private mode passes it.
    if ! mode=$(stat -c '%a' -- "$path") || [[ ! $mode =~ ^[0-7]{1,4}$ ]]; then
        printf 'refused the mode of %s could not be read, so whether it gives group or other access cannot be established\n' "$path"
        return 0
    fi
    if [[ $access != shared ]] && ((8#$mode & 8#077)); then
        printf 'refused %s is mode %s, and must give group and other no access\n' "$path" "$mode"
        return 0
    fi
    if [[ ! -r $path ]]; then
        printf 'refused %s is mode %s and owned by %s, so this account cannot read it\n' "$path" "$mode" "$(stat -c '%U' -- "$path")"
        return 0
    fi
    echo readable
}

# `secret_content_state` <path>
# `clean` where the file `secret_shape_verdict` passed holds no carriage return, `crlf` where it holds one, and `unchecked` where the check could not run.
# A FIFO or device in place of the file, or a symlink to one, answers `unchecked`, since `-D skip` passes such a file unread.
# Refused rather than left to each reader, since each reader fails on a carriage return differently.
# A sourcing shell under errexit stops on a blank CRLF line, and one without it keeps the carriage return in every value.
secret_content_state() {
    # `grep` streams the file rather than holding it, and `-D skip` keeps `grep` itself from blocking on a FIFO swapped in after the shape checks.
    local found=0
    LC_ALL=C grep -qF -D skip -- $'\r' "$1" || found=$?
    case $found in
    0) echo crlf ;;
    1)
        if [[ -f $1 ]]; then
            echo clean
        else
            echo unchecked
        fi
        ;;
    *) echo unchecked ;;
    esac
}

# `secret_content_refusal` <path> <state>
# The `refused <sentence>` for a `secret_content_state` other than `clean`, built without opening the file, and any state but `crlf` reads as `unchecked`.
secret_content_refusal() {
    if [[ $2 == crlf ]]; then
        printf 'refused %s holds a carriage return, most often from CRLF line endings, and must use LF line endings only\n' "$1"
    else
        printf 'refused the check of %s for a carriage return could not run, so whether it holds one cannot be established\n' "$1"
    fi
}

# `secret_content_verdict` <path>
# `readable` where `secret_content_state` answers `clean`, and its `secret_content_refusal` otherwise.
secret_content_verdict() {
    local state
    state=$(secret_content_state "$1")
    if [[ $state == clean ]]; then
        echo readable
    else
        secret_content_refusal "$1" "$state"
    fi
}

# `secret_verdict` <path> [private|shared]
# One word for whether <path> may be read as a secret: `secret_shape_verdict`'s answer, then `secret_content_verdict`'s where the shape is `readable`.
# A reader in another language runs this function rather than restating its checks, so the rule exists once, here.
secret_verdict() {
    local verdict
    verdict=$(secret_shape_verdict "$1" "${2:-private}")
    if [[ $verdict != readable ]]; then
        printf '%s\n' "$verdict"
        return 0
    fi
    secret_content_verdict "$1"
}

# `secret_set_verdict` <path>...
# One answer for a set of secrets read all or none, from `secret_verdict` on each path.
# `readable` where every path is, which an empty list is too.
# `missing <names>` where any is absent, naming the final component of each absent path in the order given, separated by single spaces.
# `refused <sentence>` otherwise, the first refusal in the order given, so the sentence names the first file a reader of the set would stop on.
# A missing file outranks a refusal, since the set cannot be complete whatever the refused file turns out to be.
# Naming the refusal first would send the operator to fix a mode, only for the next run to name the files still missing.
secret_set_verdict() {
    local path verdict missing="" refused=""
    for path in "$@"; do
        verdict=$(secret_verdict "$path")
        case $verdict in
        readable) ;;
        absent) missing="$missing ${path##*/}" ;;
        *) [[ -n $refused ]] || refused="$verdict" ;;
        esac
    done
    if [[ -n $missing ]]; then
        printf 'missing%s\n' "$missing"
    elif [[ -n $refused ]]; then
        printf '%s\n' "$refused"
    else
        echo readable
    fi
}

# `require_secret` <path>
# Succeeds only where `secret_verdict` answers `readable`, and otherwise prints why to stderr and fails.
# That makes a secret that must be there one bare call before the line that sources it.
# A bare call under `set -e` stops the script on that failure, where a call in an `if` or before `||` would not.
require_secret() {
    local verdict
    verdict=$(secret_verdict "$1")
    case $verdict in
    readable) return 0 ;;
    absent) printf 'ERROR: %s is not there\n' "$1" >&2 ;;
    *) printf 'ERROR: %s\n' "${verdict#refused }" >&2 ;;
    esac
    return 1
}

# `read_env_value` <env-file> <name>
# Prints <name> as the env file named by <env-file> sets it, and fails where any line of that file fails.
# A bash of its own rather than a subshell.
# That is because a caller reading this inside an `if` condition has errexit off in every subshell as well.
# For such a caller, a subshell would count only the last line of the file.
# The child also keeps `set -a` from exporting that file's credentials into anything this script spawns.
# Likewise `unset` keeps an operator's already-sourced shell from standing in for the file.
# The name is held in a variable of the child's own rather than read from `$2` at the end.
# The reason is that the file runs with the positional parameters in view and can change them.
# What the file itself prints goes to /dev/null rather than into the value.
# `BASH_ENV`, `POSIXLY_CORRECT` and `SHELLOPTS` are dropped so nothing but the file runs ahead of the read.
# It checks nothing about the file, so a caller runs `require_secret` on it first.
read_env_value() {
    # shellcheck disable=SC2016  # Every expansion belongs to the child bash.
    env -u BASH_ENV -u POSIXLY_CORRECT -u SHELLOPTS bash -Eeuo pipefail -c 'env_read_name=$2; unset -v "$env_read_name"; set -a; source "$1" >/dev/null; set +a; printf "%s" "${!env_read_name:-}"' _ "$1" "$2"
}
