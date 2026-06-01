# Installing moqlab With Containernet

`moqlab` is run in place with `python -m moqlab`; it is not installed as a
package. The important choice is where the Containernet Python environment
lives, because the Containernet backend must use a Python that can import
`mininet`.

## Install and Run
You can either install containernet to a different path or inside moqlab.

```bash
# Follow Containernet's own install steps.
# Then install moqlab's Python dependencies into the same venv:
path_to_containernet/venv/bin/pip install -r path_to_moqlab/requirements-dev.txt
```

Run moqlab with that Python:

```bash
sudo path_to_containernet/venv/bin/python -m moqlab run ...
```