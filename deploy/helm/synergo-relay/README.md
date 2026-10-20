# synergo-relay Helm chart

Runs the relay image (`ghcr.io/adrianshah/synergo-relay`, relay plus the
bundled dashboard) on Kubernetes: a Deployment, a Service, an optional
Ingress, and a PersistentVolumeClaim for room codes.

```bash
helm install synergo deploy/helm/synergo-relay \
  --namespace synergo --create-namespace \
  --set relay.corsOrigins=https://relay.example.com \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set 'ingress.hosts[0].host=relay.example.com' \
  --set 'ingress.hosts[0].paths[0].path=/' \
  --set 'ingress.tls[0].secretName=synergo-relay-tls' \
  --set 'ingress.tls[0].hosts[0]=relay.example.com' \
  --set-string 'ingress.annotations.nginx\.ingress\.kubernetes\.io/proxy-read-timeout=3600' \
  --set-string 'ingress.annotations.nginx\.ingress\.kubernetes\.io/proxy-send-timeout=3600'
```

Then `https://relay.example.com/` is the dashboard and teammates run
`synergo up --relay wss://relay.example.com/ws`. The bundled dashboard talks
to the relay on its own origin, so it needs no CORS entry. The relay still
refuses to boot in production without `CORS_ORIGINS`, so list its own URL when
it is the only dashboard, plus any dashboard hosted elsewhere.

## Values worth knowing

| Value | Default | What it does |
|-------|---------|--------------|
| `replicaCount` | `1` | See [More than one replica](#more-than-one-replica). |
| `image.tag` | chart `appVersion` | `edge` follows `main`. |
| `relay.env` | `production` | `SYNERGO_ENV`. |
| `relay.corsOrigins` | (required) | `CORS_ORIGINS`, comma-separated exact origins. |
| `relay.trustProxyHeader` | `null` (follows `ingress.enabled`) | Read the client IP from `X-Forwarded-For` (the ingress controller's). Set `true` behind another proxy you run; never without one, since clients could forge their address. Pair it with the proxy-hop settings in [docs/security.md](../../../docs/security.md) through `extraEnv`. |
| `relay.serveDashboard` | `true` | `false` serves the API only. |
| `relay.redisUrl` | `""` | `SYNERGO_REDIS_URL`. Prefer `relay.existingSecret` when it has a password. |
| `relay.existingSecret` | `""` | A Secret whose keys become env vars, e.g. `SYNERGO_REDIS_URL`, `GEMINI_API_KEY`. |
| `extraEnv` | `[]` | Any other relay variable. |
| `persistence.*` | 1 Gi, RWO | The room-code volume, used only with one replica. Kept on uninstall. |

Probes: liveness on `/health`, readiness on `/readyz`. The pod runs as the
image's non-root user with a read-only root filesystem. With a volume the
Deployment uses the `Recreate` strategy, so an upgrade has a few seconds of
downtime; daemons reconnect on their own, and pre-edit checks fail open
meanwhile.

## More than one replica

A room's live state (its members' sockets and recent edits) is held by one
relay process. So more than one replica needs two things, and the chart
refuses `replicaCount > 1` without the first:

1. **Redis.** Set `relay.redisUrl` (or `SYNERGO_REDIS_URL` in
   `relay.existingSecret`). Room codes, room leases and rate limits then live
   in Redis, so a code works on every pod. Use a Redis that persists to disk:
   it now holds the room codes. The PVC is dropped; each pod gets an
   `emptyDir` for its local copy.
2. **Room-affine routing.** Every WebSocket for one room (`/ws/<code>`) must
   reach the same pod. Kubernetes Services balance per connection, so do it at
   the ingress. For ingress-nginx:

   ```yaml
   ingress:
     annotations:
       nginx.ingress.kubernetes.io/upstream-hash-by: "$uri"
   ```

   Other controllers: see
   [docs/self-hosting.md](../../../docs/self-hosting.md#scaling-out).

Without the routing, members of one room can land on different pods and miss
each other's edits. The relay logs a warning naming the room when that
happens, and the room lease follows whichever pod still has members.
