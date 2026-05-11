#!/bin/bash
# Push Victron Energy Trader til Abelgard Gitea

cd /home/lars/CascadeProjects/windsurf-project

# Sett opp git hvis ikke allerede gjort
git config user.name "Lars-Petter Abelgard" 2>/dev/null || true
git config user.email "lars@abelgaard.no" 2>/dev/null || true

# Legg til remote
git remote remove origin 2>/dev/null || true
git remote add origin http://gitea.abelgaard.no:3000/lars/victron-trader.git

# Stage og commit
git add -A
git commit -m "Initial: Victron Energy Trader med Abelgard-konfig" || echo "Ingen endringer å committe"

# Push - vil be om passord/token hvis ikke SSH
echo "Pusher til gitea.abelgaard.no..."
git push -u origin master

echo "Ferdig!"
