# Deploying

Any Linux host with Docker Compose on the same network as the charger: a small
VM (1 core, 1 GB) is plenty. The charger reaches the app on the LAN; drivers and
PayPal reach it through a Cloudflare Tunnel, so nothing is opened on your router.

## 1. Settings and data

```
sudo install -d -m 700 /etc/evcharge
sudo install -m 600 env.example /etc/evcharge/env        # then edit it
sudo install -d -m 755 /etc/evcharge/brand               # optional: logo for BRAND_LOGO
sudo install -d -m 700 -o 10001 -g 10001 /var/lib/evcharge
echo "OCPP_BIND=192.168.1.20" > .env                     # this host's LAN address
```

10001 is the user the app runs as in its container.

## 2. The tunnel

You need a domain on Cloudflare (the free plan is enough). From any machine with
`cloudflared`:

```
cloudflared tunnel login                     # pick your domain
cloudflared tunnel create evcharge           # writes <ID>.json
cloudflared tunnel route dns evcharge ev.example.org
```

On the host:

```
sudo install -d -m 700 /etc/cloudflared
sudo cp <ID>.json /etc/cloudflared/
sudo cp deploy/cloudflared/config.yml.example /etc/cloudflared/config.yml   # set TUNNEL_ID and your hostname
sudo chown -R 65532:65532 /etc/cloudflared && sudo sh -c 'chmod 600 /etc/cloudflared/*'
```

65532 is the user the `cloudflare/cloudflared` image runs as. The `cert.pem`
from `tunnel login` can manage every tunnel on the account and isn't needed to
run one: delete it when you're done.

The ingress rules publish only `/`, `/pay/…`, `/static/…` and
`/webhooks/paypal`; everything else gets a 404 at Cloudflare's edge. To let a
charger on 4G connect through the tunnel, see [ocpp.md](ocpp.md).

## 3. Run

```
docker compose run --rm cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
docker compose up -d --build
./deploy/cloudflared/check-public.sh https://ev.example.org     # from outside your network
```

Every line of the check should say `ok`.

## 4. Before taking real money

- Test the whole flow in the PayPal sandbox with the simulator
  ([ocpp.md](ocpp.md)) and a sandbox card.
- Point the real charger at the app and do a sandbox charge with a real car.
- Back up `/var/lib/evcharge` (the session database) and `/etc/evcharge`.
- Then switch to a Live PayPal app ([paypal.md](paypal.md)).

## Operating

The admin page, `http://<OCPP_BIND>:8081/admin` from your own network with
`ADMIN_PASSWORD`, shows the sockets and sessions, stops a charge, and authorises
a free charge limited by kWh or £ (recorded at its tariff value, no payment).
It never goes through the tunnel.

```
docker compose exec app python -m evcharge.cli list              # recent sessions
docker compose exec app python -m evcharge.cli show <session>    # one session, with its event log
docker compose exec app python -m evcharge.cli cancel <session>  # release a hold by hand
docker compose exec app python -m evcharge.cli solar on|off      # the solar-rate flag
```
