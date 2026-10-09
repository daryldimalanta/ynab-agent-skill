#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 [codex|antigravity|both] [--dry-run]"
    echo ""
    echo "Installs the ynab-agent-skill to the selected AI host."
    echo "This copies the skill instructions; it does not install the standalone application."
    echo "Requires bash."
}

TARGET=""
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        codex|antigravity|both)
            TARGET="$arg"
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg"
            usage
            exit 1
            ;;
    esac
done

if [[ -z "$TARGET" ]]; then
    echo "Error: Must specify codex, antigravity, or both."
    usage
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
SKILL_SOURCE_DIR="$SCRIPT_DIR/skills/ynab-agent-skill"
EXECUTABLE_PATH="$SCRIPT_DIR/bin/ynab-agent-skill"

if [[ ! -d "$SKILL_SOURCE_DIR" ]]; then
    echo "Error: Skill source directory not found at $SKILL_SOURCE_DIR"
    exit 1
fi

TARGET_DIRS=()
if [[ "$TARGET" == "codex" || "$TARGET" == "both" ]]; then
    TARGET_DIRS+=("$HOME/.codex/skills/ynab-agent-skill")
fi
if [[ "$TARGET" == "antigravity" || "$TARGET" == "both" ]]; then
    TARGET_DIRS+=("$HOME/.gemini/config/skills/ynab-agent-skill")
fi

BACKUP_BASE="$HOME/.ynab-agent-skill-backups/$(date +%Y%m%d_%H%M%S)"

for DEST_DIR in "${TARGET_DIRS[@]}"; do
    echo "==> Preparing installation for: $DEST_DIR"
    
    LEGACY_DIR="$(dirname "$DEST_DIR")/ynab-codex-skill"
    
    if [[ -L "$DEST_DIR" || -L "$LEGACY_DIR" ]]; then
        echo "Error: Destination or legacy directory is a symlink. Unsafe to proceed."
        exit 1
    fi
    
    STAGING_DIR="$(mktemp -d)"
    if (( DRY_RUN )); then
        echo "[DRY RUN] Would create staging directory and copy skill files."
    else
        cp -R "$SKILL_SOURCE_DIR/"* "$STAGING_DIR/"
        
        # Ensure references directory exists
        mkdir -p "$STAGING_DIR/references"
    fi
    
    # Backup and restore logic
    PREF_RESTORED=0
    
    if [[ -d "$LEGACY_DIR" ]]; then
        echo "Found legacy skill at $LEGACY_DIR. Planning backup."
        if (( DRY_RUN )); then
            echo "[DRY RUN] Would move $LEGACY_DIR to $BACKUP_BASE/ynab-codex-skill_legacy"
        else
            mkdir -p "$BACKUP_BASE"
            mv "$LEGACY_DIR" "$BACKUP_BASE/ynab-codex-skill_legacy"
            if [[ -f "$BACKUP_BASE/ynab-codex-skill_legacy/references/local-preferences.md" ]]; then
                cp "$BACKUP_BASE/ynab-codex-skill_legacy/references/local-preferences.md" "$STAGING_DIR/references/local-preferences.md"
                PREF_RESTORED=1
                echo "Restored private preferences from legacy installation."
            fi
        fi
    fi
    
    if [[ -d "$DEST_DIR" ]]; then
        echo "Found existing installation at $DEST_DIR. Planning backup."
        if (( DRY_RUN )); then
            echo "[DRY RUN] Would move $DEST_DIR to $BACKUP_BASE/ynab-agent-skill_existing"
        else
            mkdir -p "$BACKUP_BASE"
            mv "$DEST_DIR" "$BACKUP_BASE/ynab-agent-skill_existing"
            if (( PREF_RESTORED == 0 )) && [[ -f "$BACKUP_BASE/ynab-agent-skill_existing/references/local-preferences.md" ]]; then
                cp "$BACKUP_BASE/ynab-agent-skill_existing/references/local-preferences.md" "$STAGING_DIR/references/local-preferences.md"
                PREF_RESTORED=1
                echo "Restored private preferences from existing installation."
            fi
        fi
    fi
    
    if (( DRY_RUN == 0 && PREF_RESTORED == 0 )); then
        echo "No existing preferences found. Generating minimal executable-location preference."
        echo "Use this executable: \`$EXECUTABLE_PATH\`" > "$STAGING_DIR/references/local-preferences.md"
    fi
    
    if (( DRY_RUN )); then
        echo "[DRY RUN] Would secure preferences and move staging to $DEST_DIR"
    else
        chmod 600 "$STAGING_DIR/references/local-preferences.md"
        mkdir -p "$(dirname "$DEST_DIR")"
        mv "$STAGING_DIR" "$DEST_DIR"
        echo "Successfully installed to $DEST_DIR."
    fi
    
    if (( DRY_RUN )); then
        rm -rf "$STAGING_DIR"
    fi
    echo ""
done

if (( DRY_RUN )); then
    echo "[DRY RUN] Completed without making changes."
else
    echo "Installation complete."
fi
