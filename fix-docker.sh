#!/bin/bash
# Fiks Docker-installasjon

echo "=== Fikser Docker ==="

# 1. Sjekk om Docker er installert
if ! command -v docker &> /dev/null; then
    echo "Installerer Docker..."
    curl -fsSL https://get.docker.com | sh
fi

# 2. Legg til bruker i docker-gruppe
sudo usermod -aG docker $USER

# 3. Start Docker daemon
if ! pgrep -x "dockerd" > /dev/null; then
    echo "Starter Docker daemon..."
    sudo dockerd > /tmp/docker.log 2>&1 &
    sleep 5
fi

# 4. Verifiser
echo "=== Sjekker Docker ==="
docker --version && echo "✅ Docker OK"
docker info > /dev/null 2>&1 && echo "✅ Docker daemon kjører" || echo "❌ Docker daemon har problemer"

# 5. Kjør mock-test hvis Docker fungerer
if docker info > /dev/null 2>&1; then
    echo ""
    echo "=== Bygger container ==="
    cd /home/lars/CascadeProjects/windsurf-project
    docker compose build
    
    echo ""
    echo "=== Kjører mock-test ==="
    docker compose run --rm victron-trader python mock_test.py
else
    echo "Docker har fortsatt problemer. Sjekk /tmp/docker.log"
    cat /tmp/docker.log
fi
