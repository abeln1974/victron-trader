#!/bin/bash
# Opprett victron-trader repo på Abelgard Gitea
# Kjøres fra terminal: bash create-gitea-repo.sh

GITEA_URL="http://gitea.abelgaard.no:3000"
TOKEN="3c4843dc5ac4d93525bd2fe90d8eddac133592ad"
REPO_NAME="victron-trader"
USER="lars"

echo "Oppretter repo $REPO_NAME på $GITEA_URL..."

RESPONSE=$(curl -s -X POST "$GITEA_URL/api/v1/user/repos" \
  -H "Content-Type: application/json" \
  -H "Authorization: token $TOKEN" \
  -d "{
    \"name\": \"$REPO_NAME\",
    \"description\": \"Automatisk strømhandel med Victron ESS - kjøper billig, selger dyrt\",
    \"private\": false,
    \"auto_init\": false,
    \"default_branch\": \"master\"
  }" 2>&1)

if echo "$RESPONSE" | grep -q "already exists"; then
    echo "Repo finnes allerede. Fortsetter..."
elif echo "$RESPONSE" | grep -q '"id":'; then
    echo "Repo opprettet!"
else
    echo "Respons fra Gitea:"
    echo "$RESPONSE"
fi

echo ""
echo "Nå kan du pushe:"
echo "  git push -u origin master"
