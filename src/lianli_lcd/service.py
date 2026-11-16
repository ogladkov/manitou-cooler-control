"""CoolerControl 4.3.1 DeviceService implementation."""

import logging
import time

import grpc

from coolercontrol.device_service.v1 import (
    device_service_pb2_grpc as rpc,
    health_pb2, initialize_device_pb2, lcd_pb2, lighting_pb2, list_devices_pb2,
    reset_channel_pb2, shutdown_pb2, status_pb2,
)
from coolercontrol.models.v1 import device_pb2

from . import __version__
from .telemetry import DEVICE_ID


class DeviceService(rpc.DeviceServiceServicer):
    def __init__(self, controllers, shutdown_event, lighting=None, telemetry=None):
        self.controllers = controllers
        self.lighting = lighting
        self.telemetry = telemetry
        self.shutdown_event = shutdown_event
        self.started = time.monotonic()

    def _controller(self, request, context):
        controller = self.controllers.get(request.device_id)
        if controller is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "Unknown LCD device")
        if hasattr(request, "channel_id") and request.channel_id != "lcd":
            context.abort(grpc.StatusCode.NOT_FOUND, "Unknown LCD channel")
        return controller

    @staticmethod
    def _execute(context, callback):
        try:
            callback()
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except PermissionError as exc:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
        except Exception as exc:
            logging.exception("LCD operation failed")
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))

    def Health(self, request, context):
        warning = not self.controllers or any(c.error for c in self.controllers.values())
        if self.lighting:
            warning = (warning or bool(self.lighting.discovery_errors)
                       or any(d.error for d in self.lighting.devices.values()))
        return health_pb2.HealthResponse(
            name="Manitou LCD / RGB", version=__version__,
            status=health_pb2.HealthResponse.STATUS_WARNING if warning
            else health_pb2.HealthResponse.STATUS_OK,
            uptime_seconds=int(time.monotonic() - self.started),
        )

    def ListDevices(self, request, context):
        devices = []
        for controller in self.controllers.values():
            screen = controller.transport.screen
            model = "Lian Li UNI TL LCD" + (" Wireless" if screen.kind == "wireless" else "")
            if screen.kind == "tryx":
                model = "TRYX PANORAMA 360"
            device = device_pb2.Device(id=screen.id, name=f"{model} ({screen.topology})",
                                       uid_info=screen.id)
            device.info.model = model
            device.info.driver_info.name = "coolercontrol-lianli-lcd"
            device.info.driver_info.version = __version__
            device.info.driver_info.locations.append(f"/sys/bus/usb/devices/{screen.topology}")
            channel = device.info.channels["lcd"]
            channel.label = "LCD"
            channel.lcd_info.screen_width = screen.width
            channel.lcd_info.screen_height = screen.height
            channel.lcd_info.max_image_size_bytes = 20 * 1024 * 1024
            for name, label, image in [("image", "Image / GIF", True),
                                       ("temp", "Temperature", False),
                                       ("carousel", "Carousel", False)]:
                channel.lcd_info.lcd_modes.add(name=name, frontend_name=label,
                                              brightness=True, orientation=True, image=image)
            devices.append(device)
        if self.lighting:
            devices.extend(d.descriptor() for d in self.lighting.devices.values())
        if self.telemetry:
            devices.append(self.telemetry.descriptor())
        return list_devices_pb2.ListDevicesResponse(devices=devices)

    def InitializeDevice(self, request, context):
        if self.telemetry and request.device_id == DEVICE_ID:
            return initialize_device_pb2.InitializeDeviceResponse()
        if self.lighting and request.device_id in self.lighting.devices:
            # Initialization must not change lighting or reset any fan controller.
            return initialize_device_pb2.InitializeDeviceResponse()
        controller = self._controller(request, context)
        self._execute(context, controller.initialize)
        return initialize_device_pb2.InitializeDeviceResponse()

    def Lcd(self, request, context):
        controller = self._controller(request, context)
        if not request.HasField("setting"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Missing LCD setting")
        self._execute(context, lambda: controller.apply(request.setting))
        return lcd_pb2.LcdResponse()

    def Status(self, request, context):
        if self.telemetry and request.device_id == DEVICE_ID:
            return status_pb2.StatusResponse(status=self.telemetry.status())
        if self.lighting and request.device_id in self.lighting.devices:
            device = self.lighting.devices[request.device_id]
            result = []
            self._execute(context, lambda: result.extend(self.lighting.status(device)))
            return status_pb2.StatusResponse(status=result)
        controller = self._controller(request, context)
        if controller.error:
            context.abort(grpc.StatusCode.UNAVAILABLE, controller.error)
        return status_pb2.StatusResponse()  # LCDs do not expose fan/temperature telemetry.

    def ResetChannel(self, request, context):
        if self.lighting and request.device_id in self.lighting.devices:
            self._apply_lighting(request, context, lighting_pb2.LightingSetting(mode="off"))
            return reset_channel_pb2.ResetChannelResponse()
        controller = self._controller(request, context)
        self._execute(context, lambda: controller.apply(lcd_pb2.LcdSetting(mode="none")))
        return reset_channel_pb2.ResetChannelResponse()

    def Shutdown(self, request, context):
        self.shutdown_event.set()
        return shutdown_pb2.ShutdownResponse()

    def _apply_lighting(self, request, context, setting):
        device = self.lighting.devices.get(request.device_id) if self.lighting else None
        if device is None or request.channel_id not in device.channels:
            context.abort(grpc.StatusCode.NOT_FOUND, "Unknown RGB device/channel")
        self._execute(context, lambda: self.lighting.apply(device, request.channel_id, setting))

    def Lighting(self, request, context):
        if not request.HasField("setting"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Missing RGB setting")
        self._apply_lighting(request, context, request.setting)
        return lighting_pb2.LightingResponse()

    # Fan and custom RPCs inherit gRPC UNIMPLEMENTED. No cooling control is exposed.
