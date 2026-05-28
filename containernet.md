You are working on a minimal **Containernet** example.

Containernet is a fork of Mininet that lets Docker containers act as hosts inside an emulated network topology. In other words, instead of creating only lightweight Mininet hosts, we can create Docker-based hosts, connect them to switches, assign IP addresses, apply link delay/loss/bandwidth, and run real applications inside those containers. Containernet supports adding Docker containers to Mininet topologies, connecting them to switches or other containers, executing commands inside containers through the Mininet CLI, and applying resource limits. ([Containernet][1])

Your task is to create a **basic working Containernet application** with:

1. One server container.
2. One client container.
3. One Open vSwitch switch.
4. A simple Python TCP or HTTP server running inside the server container.
5. A client that sends a request to the server from inside the client container.
6. A Containernet topology script that starts the network, runs the apps, tests connectivity, and opens the Mininet CLI.

Use Containernet’s `addDocker(...)` pattern. The official example imports `Containernet`, `Controller`, `Docker`, `OVSSwitch`, `CLI`, and creates Docker containers as hosts based on existing Docker images. ([GitHub][2])

The project should have this structure:

```text
containernet-basic-app/
├── Dockerfile
├── server.py
├── client.py
└── topology.py
```

### Expected behavior

When I run:

```bash
sudo python3 topology.py
```

the topology should:

1. Build or use a Docker image called `basic-net-app`.
2. Create two Docker hosts:

   * `server` with IP `10.0.0.10/24`
   * `client` with IP `10.0.0.11/24`
3. Connect both to switch `s1`.
4. Start the network.
5. Start `server.py` inside the `server` container.
6. Run `client.py` inside the `client` container.
7. Show that the client successfully reaches the server.
8. Drop into the Containernet CLI so I can test commands manually, for example:

```bash
containernet> server ifconfig
containernet> client ping server
containernet> client python3 /app/client.py
```

### Important implementation details

Use a Docker image because the application dependencies must exist inside the container. A basic Docker image is enough. The Dockerfile should install Python and copy the app files into `/app`.

The application can be very simple:

* `server.py`: starts a TCP server or HTTP server listening on `0.0.0.0`.
* `client.py`: connects to `10.0.0.10` and prints the response.

Use TCP port `5000`.

The topology should use something similar to:

```python
from mininet.net import Containernet
from mininet.node import Controller, Docker, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info
```

Then create:

```python
net = Containernet(controller=Controller, switch=OVSSwitch, link=TCLink)
net.addController("c0")
s1 = net.addSwitch("s1")
server = net.addDocker(
    "server",
    ip="10.0.0.10/24",
    dimage="basic-net-app"
)
client = net.addDocker(
    "client",
    ip="10.0.0.11/24",
    dimage="basic-net-app"
)
net.addLink(server, s1)
net.addLink(client, s1)
```

Then start the network, launch the server with `server.cmd(...)`, test the client with `client.cmd(...)`, and finally call `CLI(net)`.

The server process should run in the background inside the container, for example:

```python
server.cmd("python3 /app/server.py > /tmp/server.log 2>&1 &")
```

The client test can be:

```python
print(client.cmd("python3 /app/client.py"))
```

Finally, stop the network cleanly after the CLI exits:

```python
net.stop()
```

### Provide the complete code for all files

Generate the complete contents of:

1. `Dockerfile`
2. `server.py`
3. `client.py`
4. `topology.py`

Also include the exact commands to build and run:

```bash
docker build -t basic-net-app .
sudo python3 topology.py
```

### Constraints

Do not use Docker Compose.

Do not create a complicated multi-service architecture.

Do not assume Kubernetes.

Do not assume the app runs on the host machine. The app must run **inside the Containernet Docker hosts**.

Do not use `localhost` from the client to reach the server. The client must connect to the server container’s Containernet IP address, `10.0.0.10`.

Make the code robust enough that if the server is not ready immediately, the client waits briefly or retries.

---

## What the LLM agent should understand

The key idea is this:

```text
Host machine
└── Containernet process
    ├── Docker container: server, IP 10.0.0.10
    │   └── runs server.py
    ├── Docker container: client, IP 10.0.0.11
    │   └── runs client.py
    └── Open vSwitch switch s1
        ├── connected to server
        └── connected to client
```

The application does **not** run directly on Ubuntu. It runs inside Docker containers that Containernet treats as network hosts. That is the whole point of using Containernet rather than plain Mininet: the emulated hosts can be real Docker containers with real application dependencies. Containernet’s own documentation describes it as Mininet extended with Docker-container hosts for emulated network topologies. ([Containernet][1])

A minimal expected implementation would look like this.

---

## `Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY server.py /app/server.py
COPY client.py /app/client.py

EXPOSE 5000

CMD ["/bin/bash"]
```

---

## `server.py`

```python
import socket

HOST = "0.0.0.0"
PORT = 5000

def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((HOST, PORT))
        server_sock.listen(5)

        print(f"Server listening on {HOST}:{PORT}", flush=True)

        while True:
            conn, addr = server_sock.accept()
            with conn:
                data = conn.recv(4096)
                if not data:
                    continue

                message = data.decode(errors="replace").strip()
                print(f"Received from {addr}: {message}", flush=True)

                response = f"Hello from Containernet server. I received: {message}\n"
                conn.sendall(response.encode())

if __name__ == "__main__":
    main()
```

---

## `client.py`

```python
import socket
import time

SERVER_IP = "10.0.0.10"
SERVER_PORT = 5000

def main():
    last_error = None

    for attempt in range(1, 11):
        try:
            with socket.create_connection((SERVER_IP, SERVER_PORT), timeout=2) as sock:
                sock.sendall(b"hello from client")
                response = sock.recv(4096)
                print(response.decode(errors="replace").strip())
                return
        except OSError as exc:
            last_error = exc
            print(f"Attempt {attempt}: server not ready yet: {exc}")
            time.sleep(0.5)

    raise SystemExit(f"Client failed to reach server after retries: {last_error}")

if __name__ == "__main__":
    main()
```

---

## `topology.py`

```python
#!/usr/bin/env python3

from mininet.net import Containernet
from mininet.node import Controller, Docker, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


def run_topology():
    net = Containernet(
        controller=Controller,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
    )

    info("*** Adding controller\n")
    net.addController("c0")

    info("*** Adding switch\n")
    s1 = net.addSwitch("s1")

    info("*** Adding Docker hosts\n")
    server = net.addDocker(
        "server",
        ip="10.0.0.10/24",
        dimage="basic-net-app",
    )

    client = net.addDocker(
        "client",
        ip="10.0.0.11/24",
        dimage="basic-net-app",
    )

    info("*** Adding links\n")
    net.addLink(server, s1, cls=TCLink, delay="10ms")
    net.addLink(client, s1, cls=TCLink, delay="10ms")

    info("*** Starting network\n")
    net.start()

    info("*** Testing ping connectivity\n")
    net.ping([server, client])

    info("*** Starting server inside server container\n")
    server.cmd("python3 /app/server.py > /tmp/server.log 2>&1 &")

    info("*** Running client inside client container\n")
    output = client.cmd("python3 /app/client.py")
    print(output)

    info("*** Entering Containernet CLI\n")
    info("*** Try: client ping server\n")
    info("*** Try: client python3 /app/client.py\n")
    info("*** Try: server cat /tmp/server.log\n")
    CLI(net)

    info("*** Stopping network\n")
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run_topology()
```

---

## Commands to run

From the project directory:

```bash
docker build -t basic-net-app .
sudo python3 topology.py
```

Inside the Containernet CLI:

```bash
client ping server
client python3 /app/client.py
server cat /tmp/server.log
```

The most common mistake is using `localhost` in the client. Inside the client container, `localhost` means “the client container itself,” not the server. The client must connect to the server container’s emulated network IP, here `10.0.0.10`.

[1]: https://containernet.github.io/?utm_source=chatgpt.com "Containernet | Use Docker containers as hosts in Mininet ..."
[2]: https://github.com/containernet/containernet/blob/master/examples/dockerhosts.py?utm_source=chatgpt.com "containernet/examples/dockerhosts.py at master"
