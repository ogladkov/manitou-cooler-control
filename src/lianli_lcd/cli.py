import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading

import grpc

from coolercontrol.device_service.v1 import device_service_pb2_grpc

from .controller import Controller
from .tryx_controller import TryxController
from .tryx_transport import TryxTransport, SimulatedTryxTransport
from .discovery import discover, simulated_screens
from .service import DeviceService
from .transport import LcdTransport, SimulatedTransport
from .rgb import Lighting
from .telemetry import Telemetry
from .openai_limits import DEFAULT_LIMITS_FILE, export_limits


def serve(args):
    socket = args.socket.absolute()
    socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep a process lock before gRPC can replace an existing socket.
    with socket.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        screens = simulated_screens(args.simulate_tryx) if args.simulate else discover()
        controllers = {}
        for screen in screens:
            if screen.kind == "tryx":
                transport = (SimulatedTryxTransport(screen) if args.simulate
                             else TryxTransport(screen))
                controller = TryxController(transport, args.image_root)
            else:
                transport = (SimulatedTransport(screen) if args.simulate
                             else LcdTransport(screen, Path("/run/cc-lianli-lcd/devices")))
                controller = Controller(transport, args.image_root)
            controllers[screen.id] = controller
        lighting = Lighting(simulate=args.simulate)
        lighting.discover()
        telemetry = Telemetry(simulate=args.simulate, limits_file=args.limits_file)
        telemetry.start()
        server = grpc.server(ThreadPoolExecutor(max_workers=8))
        device_service_pb2_grpc.add_DeviceServiceServicer_to_server(
            DeviceService(controllers, stop, lighting, telemetry), server,
        )
        if not server.add_insecure_port(f"unix:{socket}"):
            raise OSError(f"Cannot bind {socket}")
        os.chmod(socket, 0o600)
        server.start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        logging.info("Serving %d LCDs and %d RGB devices at %s (simulation=%s)",
                     len(screens), len(lighting.devices), socket, args.simulate)
        try:
            stop.wait()
        finally:
            server.stop(grace=3).wait()
            telemetry.close()
            for controller in controllers.values():
                controller.close()
            socket.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Manitou Lian Li / TRYX LCD CoolerControl plugin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Read-only discovery; does not open USB devices")
    commands.add_parser("probe-rgb", help="Query RGB hubs/groups without changing settings (root)")
    limits = commands.add_parser("export-openai-limits", help="Read Codex quota as the desktop user")
    limits.add_argument("--codex", default="codex")
    limits.add_argument("--output", type=Path, default=DEFAULT_LIMITS_FILE)
    server = commands.add_parser("serve", help="Run CoolerControl gRPC device service")
    server.add_argument("--socket", type=Path, default=Path("/run/cc-lianli-lcd/service.sock"))
    server.add_argument("--simulate", action="store_true", help="Seven simulated screens; no USB I/O")
    server.add_argument("--simulate-tryx", action="store_true",
                        help="Add a TRYX Panorama to --simulate (no USB or ADB I/O)")
    server.add_argument("--limits-file", type=Path, default=DEFAULT_LIMITS_FILE)
    server.add_argument("--image-root", type=Path, action="append",
                        default=[Path("/etc/coolercontrol"), Path("/var/lib/coolercontrol")])
    args = parser.parse_args()
    if args.command == "serve" and args.simulate_tryx and not args.simulate:
        parser.error("--simulate-tryx requires --simulate")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "export-openai-limits":
        snapshot = export_limits(args.output, args.codex)
        print(json.dumps(snapshot, indent=2))
    elif args.command == "list":
        print(json.dumps([screen.as_dict() for screen in discover()], indent=2))
    elif args.command == "probe-rgb":
        lighting = Lighting()
        lighting.discover()
        print(json.dumps({"devices": [
            {"id": d.id, "name": d.name, "channels": d.channels}
            for d in lighting.devices.values()], "errors": lighting.discovery_errors}, indent=2))
        if lighting.discovery_errors:
            raise SystemExit(1)
    else:
        serve(args)


if __name__ == "__main__":
    main()
