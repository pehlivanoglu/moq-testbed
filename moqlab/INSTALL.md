# Installing moqlab With Containernet

`moqlab` is run in place with `python -m moqlab`; it is not installed as a
package. The important choice is where the Containernet Python environment
lives, because the Containernet backend must use a Python that can import
`mininet`.

## Option 1: Use an External Containernet venv

Use this if you already have a Containernet checkout, for example
`~/Research/Repos/containernet`.

```bash
cd /path/to/containernet
# Follow Containernet's own install steps and create/use its venv.

/path/to/containernet/venv/bin/pip install -r /path/to/moq-testbed/moqlab/requirements-dev.txt

cd /path/to/moq-testbed/moqlab
/path/to/containernet/venv/bin/python -m moqlab doctor
```

Run Containernet topologies with the same Python, usually through `sudo`:

```bash
sudo /path/to/containernet/venv/bin/python -m moqlab run -c configs/examples/linear_3r_1s.yaml
```

## Option 2: Put the Containernet venv Inside moqlab

Use this if you want the project-local venv at `moqlab/.venv`.

```bash
cd /path/to/moq-testbed/moqlab
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Install Containernet/mininet into this same .venv using Containernet's docs.
.venv/bin/python -m moqlab doctor
```

Run with:

```bash
sudo .venv/bin/python -m moqlab run -c configs/examples/linear_3r_1s.yaml
```

## Build Once

After the Python environment is ready:

```bash
python -m moqlab build moqx
python -m moqlab build images
python -m moqlab doctor -c configs/examples/linear_3r_1s.yaml
```
