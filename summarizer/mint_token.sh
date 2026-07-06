#!/usr/bin/env bash
# Mint a Vexa per-user API token (scope=tx) and write it into .env.summarizer.local.
# YOU run this from the Mac. It SSHes the NAS, calls admin-api at 127.0.0.1:8057
# (LAN-unreachable, so it must run on the NAS) using ADMIN_API_TOKEN from the NAS .env,
# then writes VEXA_API_KEY=<new token> into the gitignored env file, replacing any prior value.
# The token value is never printed — only its prefix + length.
#
# Why: the api-gateway's GET /meetings + /transcripts require a per-user token in the
# api_tokens table (scope "tx"), NOT the admin token. See tests/test_vexa.py + vexa.py.
set -euo pipefail

REPO="/Users/renzof/Documents/GitHub/ZZZ/discord-vexa-bridge"
ENVF="$REPO/.env.summarizer.local"
NAS="${NAS_HOST:-renzof@renzof-nas.local}"
NAS_ENV="${NAS_ENV:-/volume1/vexa/.env}"
ADMIN_PORT="${ADMIN_API_PORT:-8057}"
USER_ID="${1:-1}"

[ -f "$ENVF" ] || { echo "run summarizer/setup_env.sh first (no $ENVF)" >&2; exit 1; }

# Mint on the NAS (admin-api is bound to 127.0.0.1, so the curl must run on the NAS).
raw="$(ssh "$NAS" "
set -a; . '$NAS_ENV'; set +a
curl -s -X POST -H 'X-Admin-API-Key: '\$ADMIN_API_TOKEN \\
  'http://127.0.0.1:$ADMIN_PORT/admin/users/$USER_ID/tokens?scopes=tx&name=vexa-summarizer'
")"

tok="$(printf '%s' "$raw" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception as e:
    print("ERR: non-JSON response:", e); sys.exit(1)
tok = d.get("token") if isinstance(d, dict) else None
if not tok:
    print("ERR: no token in response:", json.dumps(d)[:300] if isinstance(d, dict) else d); sys.exit(1)
print(tok)
')"

case "$tok" in
    ERR:*) echo "$tok" >&2; echo "raw response was: $raw" >&2; exit 1 ;;
esac

# Replace existing VEXA_API_KEY or append.
python3 - "$ENVF" "$tok" <<'PY'
import sys
envf, tok = sys.argv[1], sys.argv[2]
lines = []
found = False
for l in open(envf):
    if l.startswith("VEXA_API_KEY="):
        lines.append(f"VEXA_API_KEY={tok}\n"); found = True
    else:
        lines.append(l)
if not found:
    lines.append(f"VEXA_API_KEY={tok}\n")
open(envf, "w").writelines(lines)
print(f"VEXA_API_KEY written to {envf}  |  token: {tok[:7]}...{tok[-4:]}, {len(tok)} chars")
PY

# Verify against the api-gateway (codes only, key not printed).
set -a; . "$ENVF"; set +a
code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 -H "X-API-Key: $VEXA_API_KEY" "$VEXA_API_URL/meetings")"
echo "verify GET /meetings with new token -> HTTP $code"
[ "$code" = "200" ] || echo "(non-200 — paste this to Claude; vexa.py may need a tweak)"