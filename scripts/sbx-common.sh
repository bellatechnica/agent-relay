# Shared logic for the Agent Relay sandbox launchers.
#
# Source this from a launcher; it expects AGENT_KIND (claude|codex) and
# PROFILE_FILE_NAME (settings.json|config.toml) to be set beforehand, and
# leaves the resolved values in the variables documented at each step.

set -euo pipefail

relay_usage() {
    printf 'usage: %s [--profile-dir DIR] [%s_ARG ...]\n' "$(basename "$0")" \
        "$(printf '%s' "$AGENT_KIND" | tr '[:lower:]' '[:upper:]')" >&2
    printf '\nDIR is a directory holding %s, mounted read-only. Without it the\n' \
        "$PROFILE_FILE_NAME" >&2
    printf 'sandbox runs the agent on its own defaults and nothing is mounted.\n' >&2
}

relay_die() {
    printf '%s\n' "$1" >&2
    exit "${2:-2}"
}

# Consumes an optional leading --profile-dir, leaving the rest for the agent.
# Resolves: profile_dir, profile_file, profile_name (all empty when absent)
# and relay_agent_args.
relay_parse_args() {
    profile_dir=
    profile_file=
    profile_name=
    local raw=

    case "${1:-}" in
        --profile-dir)
            [[ $# -ge 2 ]] || relay_die "--profile-dir needs a directory"
            raw=$2
            shift 2
            ;;
        --profile-dir=*)
            raw=${1#--profile-dir=}
            shift
            ;;
        -h|--help)
            relay_usage
            exit 0
            ;;
    esac
    relay_agent_args=("$@")

    [[ -n "$raw" ]] || return 0

    profile_dir=$(readlink -f -- "$raw") \
        || relay_die "cannot resolve profile directory: $raw"
    profile_file="$profile_dir/$PROFILE_FILE_NAME"
    [[ -f "$profile_file" ]] \
        || relay_die "profile file not found: $profile_file"
    profile_name=${profile_dir##*/}
    [[ -n "$profile_name" ]] || relay_die "profile directory has no name: $raw"

    # The agent's own host config directory holds credentials and session
    # history; mounting it would hand both to the sandbox.
    local native
    for native in "$HOME/.claude" "$HOME/.codex" "$HOME/.config/opencode"; do
        if [[ "$profile_dir" == "$(readlink -f -- "$native" 2>/dev/null)" ]]; then
            printf 'warning: %s is the host agent home; it contains credentials that this mount exposes to the sandbox\n' \
                "$profile_dir" >&2
        fi
    done
}

# Resolves: workspace_dir, workspace_name
relay_resolve_workspace() {
    if ! workspace_dir=$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null); then
        workspace_dir=$PWD
    fi
    workspace_dir=$(readlink -f -- "$workspace_dir")
    workspace_name=${workspace_dir##*/}
    [[ -n "$workspace_name" ]] \
        || relay_die "cannot name a sandbox for workspace: $workspace_dir"
}

# Resolves: relay_root (the checkout holding this script)
relay_resolve_root() {
    local script_path
    script_path=$(readlink -f -- "${BASH_SOURCE[1]}") \
        || relay_die "cannot resolve launcher path"
    relay_root=$(readlink -f -- "$(dirname -- "$script_path")/..")
    [[ -d "$relay_root/skills/agent-relay-message" ]] \
        || relay_die "Agent Relay skill not found under: $relay_root"
    [[ -f "$relay_root/examples/claude-mcp.json" ]] \
        || relay_die "Agent Relay MCP example not found under: $relay_root"
}

# Docker Sandboxes may be the Windows build reached from WSL. That build takes
# Windows host paths and exposes a WSL path /mnt/<drive>/rest as /<drive>/rest
# inside the sandbox. A native Linux build uses one path for both.
relay_detect_path_mode() {
    local sbx_path sbx_target
    sbx_path=$(command -v sbx) || relay_die "sbx is not on PATH"
    sbx_target=$(readlink -f -- "$sbx_path")
    if [[ "$sbx_target" == *.exe ]]; then
        relay_path_mode=windows
    else
        relay_path_mode=native
    fi
}

relay_host_path() {
    if [[ "$relay_path_mode" == windows ]]; then
        wslpath -w -- "$1"
    else
        printf '%s' "$1"
    fi
}

relay_sandbox_path() {
    if [[ "$relay_path_mode" == windows ]]; then
        case "$1" in
            /mnt/[A-Za-z]/*) printf '%s' "${1#/mnt}" ;;
            *) relay_die "cannot map path into the sandbox: $1" ;;
        esac
    else
        printf '%s' "$1"
    fi
}

# Mirrors Docker Sandboxes' own <agent>-<workdir> default, with the profile
# segment added only when a profile directory was given, so one workspace can
# hold one sandbox per profile.
relay_sandbox_name() {
    local name="$AGENT_KIND-$workspace_name"
    [[ -n "$profile_name" ]] && name="$AGENT_KIND-$profile_name-$workspace_name"
    printf '%s' "$name" | tr -c 'A-Za-z0-9.+-' '-'
}

relay_sandbox_exists() {
    local names existing
    names=$(sbx ls --quiet) || relay_die "failed to list Docker sandboxes" 1
    while IFS= read -r existing; do
        [[ "$existing" == "$1" ]] && return 0
    done <<< "$names"
    return 1
}

# Prints the value of one environment variable inside the sandbox.
relay_sandbox_env() {
    local value
    value=$(sbx exec "$1" sh -c "printf %s \"\${$2:-$3}\"") \
        || relay_die "failed to resolve $2 in sandbox: $1" 1
    [[ -n "$value" ]] || relay_die "sandbox returned an empty $2: $1" 1
    printf '%s' "$value"
}

# Links the mounted skill into the agent's own skill directory, so discovery
# does not depend on which workspace the sandbox was created for.
relay_link_skill() {
    sbx exec "$1" sh -c '
        set -eu
        skills_dir=$1
        skill_source=$2
        mkdir -p "$skills_dir"
        ln -sfnT "$skill_source" "$skills_dir/agent-relay-message"
    ' sh "$2" "$3" || relay_die "failed to link the Agent Relay skill into: $2" 1
}
