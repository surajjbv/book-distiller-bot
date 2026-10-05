#!/bin/bash
# Personal-data gate for a commit: every personal value in the bots' .env files is searched for in what is about
# to be committed (git grep --cached, case-insensitive). Exits 1 on any hit, naming the .env key and the file.
# Use it so a hit stops the commit:   bash ~/Code/botkit/pii-check.sh && git commit ...
set -u
repo=${1:-.}
# Public settings, not personal (they live in config.json now or are defaults).
PUBLIC='^(MODEL|MODEL_FALLBACK|RUN_TIMES|DRY_RUN|EMAIL_LOOKBACK_DAYS|WHATSAPP_LOOKBACK_DAYS|FIRST_RUN_HOURS|MAX_INBOX|MAX_SPAM|CHROME_PATH|WA_SHARE_DIR|DEBUG_CAPTCHA)$'
# Values also given in a .env.example are placeholders or defaults, so public too.
examples=$(cat "$(dirname "$0")"/../*/.env.example 2>/dev/null | sed -n 's/^[A-Z][A-Z0-9_]*=//p')
hits=0; n=0
for env in "$(dirname "$0")"/../*/.env; do
  while IFS= read -r line; do
    [[ $line =~ ^[[:space:]]*([A-Z][A-Z0-9_]*)=(.*)$ ]] || continue
    key=${BASH_REMATCH[1]}; val=${BASH_REMATCH[2]}
    val=${val%\"}; val=${val#\"}; val=${val%\'}; val=${val#\'}; val=$(echo "$val" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    [[ $key =~ $PUBLIC ]] && continue
    (( ${#val} < 4 )) && continue
    grep -qxF -e "$val" <<< "$examples" && continue
    n=$((n + 1))
    files=$(git -C "$repo" grep --cached -l -i -F -e "$val" 2>/dev/null)
    [ -n "$files" ] && { echo "PERSONAL DATA: value of $key (from $(basename "$(dirname "$env")")/.env) found in: $(echo $files)"; hits=1; }
  done < "$env"
done
[ $hits = 0 ] && echo "pii-check: OK ($n personal values checked against the staged tree of $(cd "$repo" && basename "$PWD"))"
exit $hits
