#!/bin/bash
# Setter opp rootless Docker - ingen sudo nødvendig etterpå
# Kjør én gang, så fungerer Docker for deg uten sudo

set -e

echo "=== Setter opp rootless Docker for $USER ==="

# 1. Sjekk om allerede satt opp
if command -v docker &> /dev/null && docker info &> /dev/null; then
    echo "Docker fungerer allerede uten sudo!"
    docker --version
    exit 0
fi

# 2. Installer dependencies (krever sudo én gang)
echo "Installerer avhengigheter (krever sudo én gang)..."
sudo apt-get update
sudo apt-get install -y uidmap dbus-user-session fuse-overlayfs slirp4netns

# 3. Installer rootless Docker
echo "Installerer rootless Docker..."
curl -fsSL https://get.docker.com/rootless | sh

# 4. Sett miljøvariabler
echo "Konfigurerer miljø..."
cat >> ~/.bashrc << 'EOF'

# Docker rootless
export PATH=$HOME/bin:$PATH
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock
EOF

export PATH=$HOME/bin:$PATH
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock

# 5. Start Docker daemon
systemctl --user start docker
systemctl --user enable docker

# 6. Verifiser
echo ""
echo "=== Verifiserer Docker ==="
docker --version
docker compose version
docker info | head -5

echo ""
echo "✅ Rootless Docker er installert!"
echo ""
echo "Logg ut og inn igjen, eller kjør:"
echo "  source ~/.bashrc"
echo ""
echo "Deretter kan du kjøre:"
echo "  cd /home/lars/CascadeProjects/windsurf-project"
echo "  docker compose build"
echo "  docker compose run --rm victron-trader python mock_test.py"
