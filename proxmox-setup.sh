#!/bin/bash
# Oppsett av victron-trader på Proxmox - trygge alternativer
# Kjør dette på PVE1, PVE2, PVE3 eller PVE4

set -e

# Konfigurasjon
VM_ID=${VM_ID:-201}
VM_NAME="victron-trader"
STORAGE=${STORAGE:-local-zfs}
BRIDGE=${BRIDGE:-vmbr0}

# Sjekk om vi er på Proxmox host
if [ ! -f /etc/pve/pve.cfg ]; then
    echo "Feil: Dette skriptet må kjøres på Proxmox VE host (PVE1/PVE2/PVE3/PVE4)"
    exit 1
fi

echo "=========================================="
echo "Victron Trader - Proxmox Oppsett"
echo "=========================================="
echo ""
echo "Velg deployment-metode:"
echo "1) LXC Container med Docker (anbefalt - lettest)"
echo "2) Dedikert Ubuntu VM med Docker (mest isolert)"
echo ""
read -p "Valg (1/2): " CHOICE

case $CHOICE in
    1)
        echo ""
        echo "=== LXC Container med Docker ==="
        echo "Oppretter privileged LXC container..."
        
        # Sjekk om template finnes
        TEMPLATE="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
        if ! pveam list local | grep -q "debian-12"; then
            echo "Laster ned Debian 12 template..."
            pveam download local debian-12-standard_12.7-1_amd64.tar.zst
        fi
        
        # Opprett LXC (privileged for Docker)
        pct create $VM_ID $TEMPLATE \
            --hostname $VM_NAME \
            --ostype debian \
            --cores 1 \
            --memory 512 \
            --swap 512 \
            --net0 name=eth0,bridge=$BRIDGE,ip=dhcp \
            --storage $STORAGE \
            --rootfs ${STORAGE}:8 \
            --features nesting=1,keyctl=1
        
        echo "Starting container..."
        pct start $VM_ID
        sleep 5
        
        echo "Installerer Docker..."
        pct exec $VM_ID -- bash -c "
            apt-get update && \
            apt-get install -y ca-certificates curl gnupg git && \
            install -m 0755 -d /etc/apt/keyrings && \
            curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
            chmod a+r /etc/apt/keyrings/docker.gpg && \
            echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable' > /etc/apt/sources.list.d/docker.list && \
            apt-get update && \
            apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin && \
            usermod -aG docker root && \
            systemctl enable docker
        "
        
        echo ""
        echo "LXC Container $VM_ID ($VM_NAME) er klar!"
        echo ""
        echo "Neste steg:"
        echo "1. pct exec $VM_ID -- bash"
        echo "2. cd /opt && git clone https://gitea.abelgaard.no:3000/lars/victron-trader.git"
        echo "3. cd victron-trader && cp .env.example .env"
        echo "4. # Rediger .env med VICTRON_HOST"
        echo "5. docker compose up -d"
        ;;
        
    2)
        echo ""
        echo "=== Dedikert Ubuntu VM ==="
        echo "Oppretter Ubuntu Server VM..."
        
        # Sjekk om ISO finnes
        ISO_PATH="local:iso/ubuntu-22.04.4-live-server-amd64.iso"
        if ! ls /var/lib/vz/template/iso/ | grep -q "ubuntu"; then
            echo ""
            echo "⚠️  Ubuntu Server ISO ikke funnet!"
            echo "Last ned fra: https://ubuntu.com/download/server"
            echo "Legg i: /var/lib/vz/template/iso/"
            echo ""
            echo 
