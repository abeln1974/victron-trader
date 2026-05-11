# Trygg Proxmox-oppsett for Victron Trader

## Problem: Docker på PVE host = Utrygt

Å kjøre Docker direkte på Proxmox VE host (PVE1, PVE2, etc.) er **ikke anbefalt**:
- Sikkerhetsrisiko (container escape, root-privilegier)
- Kan ødelegge PVE ved oppdateringer
- Hypervisor skal kun kjøre VMs og LXCs, ikke applikasjoner

## Løsning: Privileged LXC Container

### Fordeler
- ✅ Synlig i Proxmox UI (som CT 201)
- ✅ Backup via Proxmox
- ✅ Resource limits (CPU/RAM)
- ✅ Docker kjører inne i container (isolert)
- ✅ Lett å migrere mellom noder

### Oppsett på PVE1

```bash
# SSH til PVE1
ssh root@10.10.10.x  # eller 10.10.10.1 hvis CCR2004 er gateway

# 1. Last ned Debian 12 template (hvis ikke finnes)
pveam download local debian-12-standard_12.7-1_amd64.tar.zst

# 2. Opprett privileged LXC (ID 201)
pct create 201 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname victron-trader \
  --ostype debian \
  --cores 1 \
  --memory 512 \
  --swap 512 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --storage local-zfs \
  --rootfs local-zfs:8 \
  --features nesting=1,keyctl=1

# 3. Start container
pct start 201

# 4. Installer Docker inne i container
pct exec 201 -- bash -c "
  apt-get update && \
  apt-get install -y ca-certificates curl gnupg git && \
  install -m 0755 -d /etc/apt/keyrings && \
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
  chmod a+r /etc/apt/keyrings/docker.gpg && \
  echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable' > /etc/apt/sources.list.d/docker.list && \
  apt-get update && \
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
"

# 5. Klon repo og start
git clone https://gitea.abelgaard.no:3000/lars/victron-trader.git
cp victron-trader/.env.example victron-trader/.env
# Rediger .env med VICTRON_HOST=192.168.1.x
pct exec 201 -- bash -c "cd /opt && git clone https://gitea.abelgaard.no:3000/lars/victron-trader.git"
pct exec 201 -- bash -c "cd /opt/victron-trader && docker compose up -d"
```

### Styring etter oppsett

```bash
# Se logger
pct exec 201 -- docker logs victron-trader-victron-trader-1

# Restart
pct exec 201 -- docker compose -f /opt/victron-trader/docker-compose.yml restart

# Stopp
pct exec 201 -- docker compose -f /opt/victron-trader/docker-compose.yml down

# Fra Proxmox UI: Klikk på CT 201 → Console
```

## Alternativ 2: Dedikert VM (maks isolasjon)

Hvis du vil ha **maksimal isolasjon**, bruk en dedikert VM:

```bash
# Opprett Ubuntu Server VM
qm create 202 --name victron-trader-vm --memory 1024 --cores 1 \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci
qm set 202 --scsi0 local-zfs:16
qm set 202 --ide2 local:iso/ubuntu-22.04.4-live-server-amd64.iso,media=cdrom
qm set 202 --boot order=ide2;scsi0
qm start 202
# Installer Ubuntu, deretter Docker
```

## Anbefaling for Abelgard

Bruk **LXC Container (privileged)** på PVE1:
- Lett å sette opp
- Ressurs-effektivt (deler kernel med host)
- Full isolasjon av Docker
- Synlig og håndterbart i Proxmox UI

**Ikke** installer Docker direkte på PVE1/PVE2/PVE3/PVE4 host!
