# Shared logic for the Agent Relay sandbox launchers.
#
# Source this from a launcher; it expects AGENT_KIND (claude|codex|agy) and
# PROFILE_FILE_NAME (settings.json|config.toml) to be set beforehand, and
# leaves the resolved values in the variables documented at each step.

set -euo pipefail

relay_usage() {
    printf 'usage: %s [--profile-dir DIR] [--name NAME] [--with-proxy] [%s_ARG ...]\n' \
        "$(basename "$0")" \
        "$(printf '%s' "$AGENT_KIND" | tr '[:lower:]' '[:upper:]')" >&2
    printf '\nDIR is a directory holding %s, mounted read-only. Without it the\n' \
        "$PROFILE_FILE_NAME" >&2
    printf 'sandbox runs the agent on its own defaults and nothing is mounted.\n' >&2
    printf '\n--with-proxy keeps the sandbox HTTP(S)_PROXY variables that route every\n' >&2
    printf 'request through the Docker Sandbox TLS-terminating proxy. The default is\n' >&2
    printf 'to clear them: that intercept has broken long streamed responses, and it\n' >&2
    printf 'is not what provides egress containment. Applies at sandbox create only.\n' >&2
    printf '\nNAME is the sandbox name, as passed to sbx --name. Without it the name\n' >&2
    printf 'is derived from the agent, profile and workspace. Two launches that\n' >&2
    printf 'resolve to one name share one sandbox, so an explicit name is how you\n' >&2
    printf 'keep otherwise identical launches apart, or rejoin a sandbox whose\n' >&2
    printf 'derived name has since changed.\n' >&2
}

relay_die() {
    printf '%s\n' "$1" >&2
    exit "${2:-2}"
}

# Consumes the optional leading --profile-dir and --name in any order, leaving
# the rest for the agent. Resolves: profile_dir, profile_file, profile_name (all
# empty when absent), sandbox_name_override, and relay_agent_args.
relay_parse_args() {
    profile_dir=
    profile_file=
    profile_name=
    sandbox_name_override=
    relay_with_proxy=
    local raw=

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --profile-dir)
                [[ $# -ge 2 ]] || relay_die "--profile-dir needs a directory"
                raw=$2
                shift 2
                ;;
            --profile-dir=*)
                raw=${1#--profile-dir=}
                shift
                ;;
            --name)
                [[ $# -ge 2 ]] || relay_die "--name needs a sandbox name"
                sandbox_name_override=$2
                shift 2
                ;;
            --name=*)
                sandbox_name_override=${1#--name=}
                shift
                ;;
            --with-proxy)
                relay_with_proxy=1
                shift
                ;;
            -h|--help)
                relay_usage
                exit 0
                ;;
            *)
                break
                ;;
        esac
    done
    relay_agent_args=("$@")

    if [[ -n "$sandbox_name_override" ]]; then
        # Reject rather than sanitise: a silently rewritten name would leave the
        # caller resuming a sandbox it cannot name, and every later lookup would
        # miss.
        [[ "$sandbox_name_override" == "$(printf '%s' "$sandbox_name_override" \
            | tr -c 'A-Za-z0-9.+-' '-')" ]] \
            || relay_die "sandbox name may use only A-Za-z0-9.+- : $sandbox_name_override"
    fi

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
    for native in "$HOME/.claude" "$HOME/.codex" "$HOME/.config/opencode" \
        "$HOME/.gemini" "$HOME/.gemini/antigravity-cli"; do
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
    if [[ -n "${sandbox_name_override:-}" ]]; then
        printf '%s' "$sandbox_name_override"
        return 0
    fi
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

# Read-only check for a sandbox this launcher did not just create. The link is
# written once, at create, from the mounts decided at that moment; mounts never
# change afterwards, so a link that has gone missing or dangling means the
# sandbox was created by a launcher with a different Agent Relay root. Rewriting
# it here is what used to corrupt such a sandbox silently, so report instead.
relay_verify_skill() {
    sbx exec "$1" sh -c '
        set -eu
        link=$1/agent-relay-message
        if [ ! -e "$link" ]; then
            echo "Agent Relay skill missing or dangling in this sandbox:" >&2
            echo "  $link" >&2
            echo "It is written once when the sandbox is created, from the" >&2
            echo "mounts chosen then. This sandbox was created by a launcher" >&2
            echo "with a different Agent Relay root, or its skills were" >&2
            echo "removed. Recreate the sandbox, or use the launcher it was" >&2
            echo "created with. This launcher will not rewrite the link." >&2
            exit 1
        fi
    ' sh "$2" || relay_die "Agent Relay skill is not usable in sandbox: $1" 1
}

# Links the mounted skill into the agent's own skill directory, so discovery
# does not depend on which workspace the sandbox was created for. Called only
# when the sandbox is created, by the invocation that chose its mounts.
relay_link_skill() {
    sbx exec "$1" sh -c '
        set -eu
        skills_dir=$1
        skill_source=$2
        # Refuse rather than link to a path this sandbox cannot see. ln succeeds
        # on a dangling target, so an unchecked link fails silently: the agent
        # keeps a skill entry that resolves to nothing. This bites when a
        # sandbox was created by a launcher whose Agent Relay root was mounted
        # somewhere else, because the root is derived from the launcher path and
        # an existing sandbox is never re-mounted.
        if [ ! -e "$skill_source" ]; then
            echo "Agent Relay skill not readable inside this sandbox:" >&2
            echo "  $skill_source" >&2
            echo "This sandbox does not mount the Agent Relay root that this" >&2
            echo "launcher resolves to. Use the launcher the sandbox was" >&2
            echo "created with, or recreate the sandbox." >&2
            exit 1
        fi
        mkdir -p "$skills_dir"
        ln -sfnT "$skill_source" "$skills_dir/agent-relay-message"
    ' sh "$2" "$3" || relay_die "failed to link the Agent Relay skill into: $2" 1
}
