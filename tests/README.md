# Running the tests

The same test suite runs in two environments.

## 1. Plain Python (any 3.12+), no Home Assistant

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.test.txt
.venv/bin/python -m pytest
```

Runs the pure tests in `tests/*.py` (parsers, state machines, registry
decisions, migration logic). `tests/ha/` is skipped automatically because
`pytest-homeassistant-custom-component` is not installed
(`tests/conftest.py`).

## 2. Home Assistant runtime (Python 3.14, HA 2026.9.3)

```sh
pip install -r requirements.test.txt -r requirements.test-ha.txt
python3 -m pytest
```

Additionally runs `tests/ha/`: config flow, options, reauth, reconfigure,
entry migration, setup/unload, coordinator state machine, switch, device
sync and cleanup, against a real Home Assistant core. The AC is never
contacted; `RltechClient.fetch_snapshot` is replaced by a scripted fake
(`tests/ha/conftest.py`), and MQTT/TCP probes are patched.

The maintainers run this in an isolated container built from the
Home Assistant 2026.9.3 image (see the stage 4 hand-off notes); the
repository is copied in with `tar` and pytest is run in `/work`.

## Configuration

`setup.cfg` (`[tool:pytest]`) sets `asyncio_mode = auto` (needed by the
async autouse fixtures of pytest-homeassistant-custom-component) and
`pythonpath = .` (so `custom_components.rltech_fttr` is importable).

## Frontend checks

`tests/test_frontend.py` runs `node --check` on every file in
`custom_components/rltech_fttr/www/` when Node is installed, and a DOM smoke
test of the panel and both cards (`tests/js/smoke.cjs`) when jsdom is also
available:

```sh
npm install --prefix /tmp/rltech-js jsdom@24
NODE_PATH=/tmp/rltech-js/node_modules python3 -m pytest tests/test_frontend.py
```

Both are skipped where Node is missing (for example the HA test container).
