"""Utility to relay display VSync events to a serial-connected DLP-Link emitter.

The script mimics the quick prototype shared in the support discussion by
allowing a Windows machine to call into the `VSYNCWaiter.dll` helper, wait for
vertical-sync events, and forward trigger pulses to a microcontroller over a
serial link.  Command-line options match the ones printed in the user error
message (`--port`, `--dll`, `--baudrate`, `--pulse-threshold`, and
`--warmup`).

The new script offers an ergonomic quality-of-life improvement: arguments can
be omitted if defaults are provided inside a JSON config file.  This removes
friction for the user who simply wants to type `python vsync.py` and have the
same settings used every time.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any, Dict, Optional

import serial

LOGGER = logging.getLogger("vsync")
DEFAULT_CONFIG_PATH = Path(__file__).with_name("vsync.config.json")
PULSE_BYTE = b"\xAA"


def _load_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Failed to load config '{config_path}': {exc}") from exc


def _resolve_option(
    cli_value: Optional[Any],
    config: Dict[str, Any],
    key: str,
    *,
    required: bool = False,
) -> Optional[Any]:
    if cli_value is not None:
        return cli_value
    if key in config:
        return config[key]
    if required:
        raise ValueError(f"Option '{key}' is required; supply it on the command line or config file")
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Relay VSYNC triggers to a serial port")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to the JSON config file")
    parser.add_argument("--port", help="Serial port name (e.g. COM11 or /dev/ttyUSB0)")
    parser.add_argument("--dll", help="Path to VSYNCWaiter.dll")
    parser.add_argument("--baudrate", type=int, default=None, help="Serial baudrate (default 921600)")
    parser.add_argument(
        "--pulse-threshold",
        dest="pulse_threshold",
        type=float,
        default=None,
        help="Ignore duplicate VSYNC edges that occur faster than this many seconds",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=None,
        help="Number of VSYNC pulses to discard after startup before emitting pulses",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="info",
        help="Logging verbosity",
    )
    return parser


def _create_vsync_waiter(dll_path: Path):
    dll = ctypes.CDLL(str(dll_path))
    func = dll.wait_for_vsync
    func.restype = ctypes.c_int
    return func


def _open_serial(port: str, baudrate: int) -> serial.Serial:
    try:
        ser = serial.Serial(port, baudrate)
    except serial.SerialException as exc:
        raise SystemExit(f"Failed to open serial port '{port}': {exc}") from exc
    return ser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config = _load_config(args.config)

    try:
        port = _resolve_option(args.port, config, "port", required=True)
        dll_path = Path(_resolve_option(args.dll, config, "dll", required=True))
    except ValueError as exc:
        parser.error(str(exc))

    baudrate = _resolve_option(args.baudrate, config, "baudrate") or 921_600
    pulse_threshold = _resolve_option(args.pulse_threshold, config, "pulse_threshold") or 0.0005
    warmup = int(_resolve_option(args.warmup, config, "warmup") or 0)

    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s: %(message)s")

    LOGGER.info("Opening serial port %s @ %d baud", port, baudrate)
    ser = _open_serial(port, baudrate)

    LOGGER.info("Loading VSYNC waiter DLL from %s", dll_path)
    vsync_func = _create_vsync_waiter(dll_path)

    LOGGER.info("Starting VSYNC relay (pulse_threshold=%s s, warmup=%s frames)", pulse_threshold, warmup)

    try:
        for _ in range(warmup):
            vsync_func()

        last_pulse = 0.0
        while True:
            if vsync_func() != 1:
                continue
            now = time.perf_counter()
            if now - last_pulse < pulse_threshold:
                continue
            last_pulse = now
            ser.write(PULSE_BYTE)
    except KeyboardInterrupt:
        LOGGER.info("Exiting on user request")
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
