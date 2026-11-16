import os
from pathlib import Path
import subprocess
import sys

import grpc
import pytest

from coolercontrol.device_service.v1 import device_service_pb2_grpc, list_devices_pb2, shutdown_pb2


@pytest.mark.parametrize("tryx", [False, True])
def test_daemon_process_socket_permissions_single_instance_and_shutdown(tmp_path, tryx):
    socket = tmp_path / "lcd.sock"
    command = [sys.executable, "-m", "lianli_lcd.cli", "serve", "--simulate",
               "--socket", str(socket)]
    if tryx:
        command.append("--simulate-tryx")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with grpc.insecure_channel(f"unix:{socket}") as channel:
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = device_service_pb2_grpc.DeviceServiceStub(channel)
            devices = stub.ListDevices(list_devices_pb2.ListDevicesRequest()).devices
            assert any(d.id == "sim-tryx" for d in devices) == tryx
            assert os.stat(socket).st_mode & 0o777 == 0o600
            second = subprocess.run(command, capture_output=True, timeout=5)
            assert second.returncode != 0
            assert b"BlockingIOError" in second.stderr
            device_service_pb2_grpc.DeviceServiceStub(channel).Shutdown(shutdown_pb2.ShutdownRequest())
        process.wait(timeout=5)
        assert process.returncode == 0
        assert not Path(socket).exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
