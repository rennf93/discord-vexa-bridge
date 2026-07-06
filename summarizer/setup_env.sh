#!/usr/bin/env bash
# Setup .env.summarizer.local for the meeting summarizer (Mac). YOU run this.
#
# It pulls two secrets Claude is not allowed to grab autonomously:
#   * the Vexa admin token (ADMIN_API_TOKEN) from the NAS .env, via SSH
#   * the Obsidian MCP bearer token from the local vault-as-mcp plugin config
# and writes them into a gitignored .env.summarizer.local (matched by .env.*.local).
# No secret value is printed. Optional arg overrides the LLM model.
#
# LLM routing note: the local ollama proxy exposes cloud models over its OpenAI endpoint
# (/v1/chat/completions, no auth). Use litellm model id "openai/<model>:cloud" with
# AI_BASE_URL=http://localhost:11434/v1. Plain "ollama/*" ids hit /api/generate and the
# local raw models are unusable (no template); the ":cloud" models work.
set -euo pipefail

REPO="/Users/renzof/Documents/GitHub/ZZZ/discord-vexa-bridge"
ENVF="$REPO/.env.summarizer.local"
NAS="${NAS_HOST:-renzof@renzof-nas.local}"
NAS_ENV="${NAS_ENV:-/volume1/vexa/.env}"
VAULT_JSON="$HOME/Documents/Obsidian/Renn's Vault/.obsidian/plugins/vault-as-mcp/data.json"
LLM_MODEL="${1:-openai/glm-5.2:cloud}"
VEXA_URL="${VEXA_API_URL:-http://192.168.50.111:8056}"

[ -f "$VAULT_JSON" ] || { echo "vault-as-mcp config not found at $VAULT_JSON" >&2; exit 1; }

umask 077
: > "$ENVF"
{
    echo "SUMMARIZE_ENABLED=true"
    echo "AI_MODEL=$LLM_MODEL"
    echo "AI_API_KEY=not-needed"
    echo "AI_BASE_URL=http://localhost:11434/v1"
    echo "VEXA_API_URL=$VEXA_URL"
    echo "SUMMARIZE_PLATFORMS=discord"
    echo "MIN_TRANSCRIPT_SECONDS=30"
    echo "OBSIDIAN_ENABLED=true"
    echo "OBSIDIAN_NOTE_FOLDER=Meetings"
    echo "INCLUDE_TRANSCRIPT=true"
    echo "VEXA_NOTES_ENABLED=false"
    echo "DRY_RUN=false"
} >> "$ENVF"

# Vexa admin token from the NAS .env (never printed).
VEXA_KEY="$(ssh "$NAS" "grep '^ADMIN_API_TOKEN=' '$NAS_ENV' | cut -d= -f2-")"
[ -n "$VEXA_KEY" ] || { echo "could not read ADMIN_API_TOKEN from NAS" >&2; exit 1; }
echo "VEXA_API_KEY=$VEXA_KEY" >> "$ENVF"

# Obsidian MCP bearer + host:port from the local plugin config (never printed).
OBS_DATA="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d["bearerToken"], d.get("serverHost", "127.0.0.1"), d.get("serverPort", 8765))
' "$VAULT_JSON")"
read -r TOK HOST PORT <<< "$OBS_DATA"
echo "OBSIDIAN_MCP_URL=http://${HOST}:${PORT}/mcp" >> "$ENVF"
echo "OBSIDIAN_MCP_TOKEN=$TOK" >> "$ENVF"

chmod 600 "$ENVF"
echo "wrote $ENVF ($(wc -c < "$ENVF") bytes, mode $(stat -f %Lp "$ENVF"))  |  LLM: $LLM_MODEL"
echo
echo "=== NAS adapter image ==="
ssh "$NAS" 'sudo -n docker ps --format "{{.Names}} {{.Image}} {{.Status}}" 2>/dev/null | grep -i discord || echo "(need sudo on the NAS — run: ssh renzof@renzof-nas.local sudo docker ps)"' 2>&1 | head
echo
echo "=== Vexa /meetings (key not printed) ==="
set -a; . "$ENVF"; set +a
code="$(curl -s -o /tmp/vexa_meetings.json -w "%{http_code}" -H "X-API-Key: $VEXA_API_KEY" "$VEXA_API_URL/meetings")"
echo "HTTP $code"
python3 -c '
import json
try:
    d = json.load(open("/tmp/vexa_meetings.json"))
except Exception as e:
    print("non-JSON:", e); raise SystemExit
rows = d.get("meetings", d) if isinstance(d, dict) else d
if isinstance(rows, list):
    print(f"{len(rows)} meeting(s)")
    for r in rows[:25]:
        print(" ", r.get("id"), r.get("platform"), r.get("platform_specific_id"), r.get("status"), r.get("start_time"))
else:
    print("unexpected response (not a list):", json.dumps(d)[:400])
'