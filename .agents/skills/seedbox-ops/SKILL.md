---
name: seedbox-ops
description: Generic operational guide for connecting a remote seedbox over an autossh tunnel, preserving hardlinks across torrent/media directories, managing systemd user services, and troubleshooting tunnel connectivity.
---

# Seedbox Operations & Tunnel Guide

Use this reference when configuring or troubleshooting the remote seedbox connection (`SEEDBOX_HOST`, `SEEDBOX_USER`) and the multiplexed `autossh` tunnel in `docker-compose.yml`.

## 1. Seedbox Layout & Hardlink Preservation

Keep downloads and organized media libraries on the **same underlying filesystem mount** (`<seedbox-home>/`):

```text
<seedbox-home>/
├── torrents/              # qBittorrent active downloads & seeding data
│   ├── Movies/
│   ├── TV Shows/
│   └── adult/
└── media/                 # Hardlinked library roots for *arr managers & Jellyfin
    ├── Movies/            # RADARR_ROOT_FOLDER
    ├── TV Shows/          # SONARR_ROOT_FOLDER
    └── adult/             # WHISPARR_ROOT_FOLDER
```

When qBittorrent downloads and the `*arr` root folders share one mount, imports hardlink instead of copying — saving disk space while seeding continues uninterrupted.

## 2. Tunneled Service Port Mapping (Defaults)

`seedbox-tunnel` forwards internal localhost services on the seedbox (`SEEDBOX_INTERNAL_HOST`, default `127.0.0.1`) to `127.0.0.1` on the Docker host and `seedbox-tunnel:<port>` on `media-net`:

| Service | Default Port Variable | Default Port | Purpose |
|---|---|---|---|
| **qBittorrent** | `QB_PORT` / `QB_REMOTE_PORT` | `8080` | BitTorrent Web API |
| **Prowlarr** | `PROWLARR_PORT` / `PROWLARR_REMOTE_PORT` | `9696` | Indexer search & feeds |
| **Whisparr** | `WHISPARR_PORT` / `WHISPARR_REMOTE_PORT` | `6969` | Adult Vault library manager |
| **Radarr** | `RADARR_PORT` / `RADARR_REMOTE_PORT` | `7878` | Movies manager |
| **Sonarr** | `SONARR_PORT` / `SONARR_REMOTE_PORT` | `8989` | TV Shows manager |
| **cross-seed** | `CROSS_SEED_PORT` / `CROSS_SEED_REMOTE_PORT` | `2468` | Automated cross-seeding daemon |

Override any port in `.env` if your seedbox exposes services on non-default ports.

## 3. SSH Key Hygiene

Mount **only** a dedicated `seedbox-ssh/` directory (containing the seedbox client key and a pinned `known_hosts` entry) rather than the user's entire `~/.ssh`:

```bash
install -d -m 700 ~/.config/muuvies/seedbox-ssh
install -m 600 ~/.ssh/<seedbox_key> ~/.config/muuvies/seedbox-ssh/id_ed25519
ssh-keygen -F <SEEDBOX_HOST> -f ~/.ssh/known_hosts | grep -v '^#' > ~/.config/muuvies/seedbox-ssh/known_hosts
```

Both `seedbox-tunnel` and `media-search` (SFTP streaming) enforce `StrictHostKeyChecking=yes` / `known_hosts` verification and fail closed if `known_hosts` is missing.

## 4. Troubleshooting Checklist

- **Tunnel refuses to start / ExitOnForwardFailure:** Confirm your SSH key works manually (`ssh -p "${SEEDBOX_SSH_PORT:-22}" "<SEEDBOX_USER>@<SEEDBOX_HOST>" true`) and that `known_hosts` matches the server's host key.
- **Prowlarr or qBittorrent unreachable:** Check `docker compose logs --tail 50 seedbox-tunnel` and verify the remote ports on the seedbox match `*_REMOTE_PORT` in `.env`.
- **Slow or failing SFTP streaming:** Ensure `STREAM_MEDIA_ROOT` matches the directory containing hardlinked media on the seedbox (`<seedbox-home>/media`).
