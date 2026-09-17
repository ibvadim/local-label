"""Discovery, raw TSPL delivery and immediate status for server printers."""

from __future__ import annotations

import glob
import os
import re
import select
import subprocess
import sys
from pathlib import Path

from fastapi import HTTPException


# USB descriptors on budget label printers are often just "Composite Device".
# This is presentation metadata only; matching and connection remain based on
# the full discovered descriptor including serial number.
KNOWN_USB_PRINTERS = {
    (0x1203, 0x0160): "TSC TDP-225",
}


def discover_server_devices() -> list[dict[str, str]]:
    """Return destinations usable by this server; no client USB permissions needed."""
    devices: list[dict[str, str]] = []
    devices.extend(_discover_libusb_devices())
    if os.name == "nt":
        try:
            import win32print  # type: ignore[import-not-found]

            for flags, _, name, _ in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            ):
                devices.append({"uri": f"windows:{name}", "name": name, "transport": "windows"})
        except ImportError:
            # The app remains usable without the optional Windows integration.
            pass
    else:
        for path in sorted(set(glob.glob("/dev/usb/lp*") + glob.glob("/dev/lp*"))):
            devices.append({"uri": f"raw:{path}", "name": Path(path).name, "transport": "raw"})
        # CUPS is normally how macOS and network/shared printers are exposed.
        try:
            output = subprocess.run(["lpstat", "-v"], capture_output=True, text=True, timeout=3, check=False).stdout
            for line in output.splitlines():
                if line.startswith("device for ") and ": " in line:
                    queue = line[11:].split(": ", 1)[0]
                    devices.append({"uri": f"cups:{queue}", "name": queue, "transport": "cups"})
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
    # A USB printer can be visible both through libusb and CUPS.  The former is
    # the direct TSPL path, so do not present the same URI twice.
    return list({device["uri"]: device for device in devices}.values())


def server_discovery_diagnostics() -> list[str]:
    """Explain an empty server list without exposing hardware IDs to the UI."""
    messages: list[str] = []
    if not _usb_module():
        messages.append(
            "Прямой USB-поиск недоступен: на сервере не установлен PyUSB. "
            "Запустите Docker-образ или установите pyusb в окружение сервера."
        )
    if os.name == "posix" and sys.platform == "darwin":
        messages.append(
            "На macOS Docker Desktop не передаёт USB-принтеры контейнеру напрямую; "
            "для контейнера используйте CUPS или host print agent."
        )
    return messages


def status_for_device(device_uri: str) -> dict[str, str]:
    if device_uri.startswith("libusb:"):
        device = _find_libusb_device(device_uri)
        if device is None:
            return {"state": "offline", "message": "USB-принтер отключён от сервера"}
        try:
            return _libusb_status(device)
        except RuntimeError as exc:
            return {"state": "error", "message": str(exc)}
    if device_uri.startswith("raw:"):
        path = device_uri.removeprefix("raw:")
        if not Path(path).exists():
            return {"state": "offline", "message": "Устройство не найдено на сервере"}
        if not os.access(path, os.R_OK | os.W_OK):
            return {"state": "error", "message": "Нет прав на чтение/запись устройства"}
        try:
            value = _raw_status(path, timeout=0.4)
            return _decode_tspl_status(value)
        except TimeoutError:
            return {"state": "ready", "message": "Устройство доступно (статус не ответил)"}
        except OSError as exc:
            return {"state": "error", "message": f"Ошибка устройства: {exc.strerror or exc}"}
    if device_uri.startswith("cups:"):
        queue = device_uri.removeprefix("cups:")
        try:
            result = subprocess.run(["lpstat", "-p", queue], capture_output=True, text=True, timeout=3, check=False)
            text = (result.stdout + result.stderr).lower()
            if result.returncode or "disabled" in text:
                return {"state": "error", "message": result.stderr.strip() or "Очередь CUPS отключена"}
            return {"state": "ready", "message": "Очередь CUPS готова"}
        except (FileNotFoundError, subprocess.SubprocessError):
            return {"state": "offline", "message": "CUPS недоступен на сервере"}
    return {"state": "unknown", "message": "Статус проверяется драйвером Windows при отправке"}


def send_to_printer(device_uri: str, data: bytes) -> int:
    try:
        if device_uri.startswith("raw:"):
            path = device_uri.removeprefix("raw:")
            status = status_for_device(device_uri)
            if status["state"] not in {"ready", "printing"}:
                raise HTTPException(409, status["message"])
            with open(path, "wb", buffering=0) as device:
                return device.write(data)
        if device_uri.startswith("libusb:"):
            device = _find_libusb_device(device_uri)
            if device is None:
                raise HTTPException(409, "USB-принтер отключён от сервера")
            return _libusb_write(device, data)
        if device_uri.startswith("cups:"):
            queue = device_uri.removeprefix("cups:")
            result = subprocess.run(["lp", "-d", queue, "-o", "raw"], input=data, capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise HTTPException(502, result.stderr.decode(errors="replace").strip() or "CUPS не принял задание")
            return len(data)
        if device_uri.startswith("windows:"):
            import win32print  # type: ignore[import-not-found]
            name = device_uri.removeprefix("windows:")
            handle = win32print.OpenPrinter(name)
            try:
                win32print.StartDocPrinter(handle, 1, ("LocalLabel TSPL", None, "RAW"))
                win32print.StartPagePrinter(handle)
                written = win32print.WritePrinter(handle, data)
                win32print.EndPagePrinter(handle)
                win32print.EndDocPrinter(handle)
                return written
            finally:
                win32print.ClosePrinter(handle)
        raise HTTPException(422, "Неизвестный транспорт принтера")
    except HTTPException:
        raise
    except ImportError as exc:
        raise HTTPException(
            501, "Для печати в Windows установите пакет pywin32 на сервере"
        ) from exc
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise HTTPException(502, f"Не удалось отправить задание: {exc}") from exc


def _raw_status(path: str, timeout: float) -> int:
    descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(descriptor, b"\x1b!?")
        readable, _, _ = select.select([descriptor], [], [], timeout)
        if not readable:
            raise TimeoutError
        answer = os.read(descriptor, 1)
        if len(answer) != 1:
            raise TimeoutError
        return answer[0]
    finally:
        os.close(descriptor)


def _decode_tspl_status(value: int) -> dict[str, str]:
    if value == 0:
        return {"state": "ready", "message": "Готов к печати"}
    if value == 0x20:
        return {"state": "printing", "message": "Печатает"}
    messages = [(0x01, "Крышка открыта"), (0x02, "Замятие носителя"), (0x04, "Нет носителя"), (0x08, "Нет риббона"), (0x10, "Печать приостановлена")]
    found = [message for bit, message in messages if value & bit]
    return {"state": "error", "message": ", ".join(found) or f"Статус TSPL 0x{value:02X}"}


def _usb_module():
    try:
        import usb.core  # type: ignore[import-not-found]
        import usb.util  # type: ignore[import-not-found]
    except ImportError:
        return None
    return usb.core, usb.util


def _discover_libusb_devices() -> list[dict[str, str]]:
    """Discover USB printer-class interfaces without asking for VID/PID."""
    modules = _usb_module()
    if not modules:
        return []
    core, util = modules
    found: list[dict[str, str]] = []
    try:
        devices = core.find(find_all=True)
        for device in devices:
            if not _has_printer_interface(device):
                continue
            serial = _usb_string(device, device.iSerialNumber) or f"{device.bus}-{device.address}"
            product = _usb_string(device, device.iProduct)
            maker = _usb_string(device, device.iManufacturer)
            name = " ".join(part for part in (maker, product) if part).strip()
            # TSC TDP-225 commonly exposes a generic USB composite descriptor.
            if not name or name.casefold() == "composite device":
                name = KNOWN_USB_PRINTERS.get(
                    (device.idVendor, device.idProduct),
                    f"USB-принтер {device.idVendor:04X}:{device.idProduct:04X}",
                )
            found.append({
                "uri": f"libusb:{device.idVendor:04x}:{device.idProduct:04x}:{serial}",
                "name": name,
                "transport": "libusb",
            })
    except Exception:
        # Discovery must not make the printer screen unavailable when libusb is
        # missing permissions or a non-printer USB device misbehaves.
        return []
    return found


def _has_printer_interface(device) -> bool:
    try:
        for configuration in device:
            for interface in configuration:
                if interface.bInterfaceClass == 7:  # USB Printer Class
                    return True
    except Exception:
        return False
    return False


def _usb_string(device, index: int) -> str:
    modules = _usb_module()
    if not modules or not index:
        return ""
    try:
        return str(modules[1].get_string(device, index) or "")
    except Exception:
        return ""


def _find_libusb_device(device_uri: str):
    modules = _usb_module()
    if not modules:
        return None
    match = re.fullmatch(r"libusb:([0-9a-f]{4}):([0-9a-f]{4}):(.+)", device_uri)
    if not match:
        return None
    vendor, product, identity = int(match[1], 16), int(match[2], 16), match[3]
    try:
        for device in modules[0].find(find_all=True, idVendor=vendor, idProduct=product):
            serial = _usb_string(device, device.iSerialNumber) or f"{device.bus}-{device.address}"
            if serial == identity and _has_printer_interface(device):
                return device
    except Exception:
        return None
    return None


def _libusb_endpoints(device):
    modules = _usb_module()
    if not modules:
        raise RuntimeError("На сервере не установлен PyUSB")
    _, util = modules
    try:
        device.set_configuration()
    except Exception:
        pass
    try:
        configuration = device.get_active_configuration()
    except Exception as exc:
        raise RuntimeError(f"Не удалось открыть USB-принтер: {exc}") from exc
    for interface in configuration:
        if interface.bInterfaceClass != 7:
            continue
        out_endpoint = next((endpoint for endpoint in interface if util.endpoint_direction(endpoint.bEndpointAddress) == util.ENDPOINT_OUT), None)
        in_endpoint = next((endpoint for endpoint in interface if util.endpoint_direction(endpoint.bEndpointAddress) == util.ENDPOINT_IN), None)
        if out_endpoint:
            return util, interface, out_endpoint, in_endpoint
    raise RuntimeError("У USB-принтера не найден endpoint для передачи")


def _with_libusb_interface(device, operation):
    util, interface, out_endpoint, in_endpoint = _libusb_endpoints(device)
    detached = False
    try:
        if sys.platform.startswith("linux") and device.is_kernel_driver_active(interface.bInterfaceNumber):
            device.detach_kernel_driver(interface.bInterfaceNumber)
            detached = True
        util.claim_interface(device, interface.bInterfaceNumber)
        return operation(out_endpoint, in_endpoint)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Ошибка связи с USB-принтером: {exc}") from exc
    finally:
        try:
            util.release_interface(device, interface.bInterfaceNumber)
        except Exception:
            pass
        if detached:
            try:
                device.attach_kernel_driver(interface.bInterfaceNumber)
            except Exception:
                pass


def _libusb_status(device) -> dict[str, str]:
    def operation(out_endpoint, in_endpoint):
        if not in_endpoint:
            return {"state": "ready", "message": "USB-принтер доступен; ответ статуса не поддержан"}
        out_endpoint.write(b"\x1b!?", timeout=800)
        try:
            answer = bytes(in_endpoint.read(1, timeout=800))
        except Exception:
            return {"state": "ready", "message": "USB-принтер доступен; статус не ответил"}
        return _decode_tspl_status(answer[0]) if answer else {"state": "unknown", "message": "Пустой ответ статуса"}
    return _with_libusb_interface(device, operation)


def _libusb_write(device, data: bytes) -> int:
    def operation(out_endpoint, _):
        written = out_endpoint.write(data, timeout=30_000)
        if written != len(data):
            raise RuntimeError(f"Принтер принял только {written} из {len(data)} байт")
        return written
    return _with_libusb_interface(device, operation)
