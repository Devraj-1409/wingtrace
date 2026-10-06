# Putting WingTrace online

The site has two parts:

- **Website** (`frontend/`): static files. Free on **GitHub Pages**.
- **Backend** (`backend/`): a small always-on Python server. It fetches the live data (the data APIs
  block direct browser requests and have tight rate limits), works out routes and keeps each flight's
  path. GitHub Pages can't run it, so it needs a machine that stays on.

Without a backend, the GitHub Pages site shows the globe but no aircraft.

## Backend: pick one

### A. Render free plan (no card needed)

1. Sign in to [render.com](https://render.com) with GitHub.
2. **New → Blueprint**, pick this repository, **Apply**. `render.yaml` sets everything up: the
   backend in Singapore on the free plan, allowed to answer the GitHub Pages site.
3. Put the service's address (e.g. `https://wingtrace-api.onrender.com`) in
   `frontend/.env.production` as `VITE_API_BASE`, and push.

The free plan (512 MB, 0.1 CPU) sleeps after 15 minutes without visitors. The next visitor waits about
a minute while it wakes (the site says so), then planes fill in over a couple of minutes. Its disk
is wiped on each wake, so flown paths start fresh.

### B. Free cloud VM (always on; free tiers usually need a card)

1. Create a small Linux VM on a free tier (for example Oracle Cloud "Always Free"; free tiers change,
   so check the current terms). Open ports 80 and 443.
2. Get a free domain name for it, e.g. `wingtrace.duckdns.org` from [DuckDNS](https://www.duckdns.org),
   pointing at the VM's public IP.
3. On the VM, install Docker, copy this repository, then:

   ```sh
   cd deploy
   cp .env.example .env      # fill in API_DOMAIN, CORS_ORIGINS, USER_AGENT
   docker compose up -d --build
   ```

   Caddy gets an HTTPS certificate automatically. Check `https://<API_DOMAIN>/api/health`.
4. Disk: well under 1 GB (routes and airports, plus two days of flight paths).

### C. Your own PC (free, only online while it's on)

Run the backend (`start.cmd`), and expose it with a free
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).
A quick tunnel (`cloudflared tunnel --url http://localhost:8000`) gets a new random URL each time, so
the website would need rebuilding; a named tunnel (free Cloudflare account and a domain) keeps a fixed
URL. Set `CORS_ORIGINS` to your GitHub Pages address.

## Website on GitHub Pages

1. Push the repository to GitHub (it must be public for free GitHub Pages).
2. Repository **Settings → Pages → Source: GitHub Actions**.
3. Set `VITE_API_BASE` in `frontend/.env.production` to your backend's address.
4. Push to `main` (or run the "Deploy site to GitHub Pages" workflow). The site appears at
   `https://<user>.github.io/<repo>/`.

No keys or tokens are needed: all map imagery is free and loaded directly by the browser.

## Before telling people about it

- [ ] Put a contact URL or email in `USER_AGENT` and in the About page (privacy removal requests).
- [ ] Email adsb.lol: their API terms ask production users to get in touch so changes don't break you.
- [ ] Keep the adsb.fi credit and link (required by their terms) and the Sentinel-2 credit on the map
      (required by its licence).
- [ ] Everything here assumes a non-commercial site. If it ever makes money: adsb.fi's data is for
      personal, non-commercial use (set `ADSBFI_ENABLED=false`), and Sentinel-2 cloudless is
      CC BY-NC-SA (non-commercial), so the zoomed-in imagery would need replacing.
