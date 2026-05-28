# moqlab containernet topology

Runs the moqlab pub/relay/sub trio as Docker hosts inside a Containernet
emulated network, with a tunable TCLink hop between publisher and subscriber.

```
pub (10.0.0.10) ─ s1 ─[TCLink: delay,bw]─ s2 ─ sub (10.0.0.20)
                     │
                 relay (10.0.0.1)
```

## Prereqs

1. Containernet installed on the host (`from mininet.net import Containernet`
   must work for the user running the script). See
   https://github.com/containernet/containernet for the install playbook.
2. The three lab images built locally:

   ```bash
   docker compose -f moqlab/docker/docker-compose.yml build
   ```

   This produces `moqlab-relay`, `moqlab-pub`, `moqlab-sub`.

## Run

From the repo root:

```bash
sudo python3 moqlab/containernet/topology.py
```

Inside the Containernet CLI:

```text
containernet> sub tail -f /tmp/sub.log     # date strings arriving
containernet> relay tail -f /tmp/relay.log
containernet> pub  tail -f /tmp/pub.log
containernet> sub ping relay
```

`exit` or `quit` tears the topology down cleanly.

## Tuning the link

Edit `LINK_DELAY` and `LINK_BW` at the top of `topology.py` to change the
shaping on the s1↔s2 hop (the only TCLink in the topology).
