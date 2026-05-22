import math
from dataclasses import dataclass, field

# ============ Custom Exception Classes ============

class ACInfinityError(Exception):
    """Base exception for AC Infinity integration."""
    pass


class ACInfinityAuthError(ACInfinityError):
    """Authentication failure with AC Infinity API."""
    pass


class ACInfinityAPIError(ACInfinityError):
    """API communication error."""
    pass


class ACInfinityDeviceError(ACInfinityError):
    """Device not found or invalid."""
    pass


class ACInfinityAdvanceConflictError(ACInfinityDeviceError):
    """Raised when a write targets a port under Advance Automation control (modeType=15)."""
    pass


class ACInfinityConfigError(ACInfinityError):
    """Configuration or file error."""
    pass


@dataclass
class ACIReading:
    """Current sensor reading from AC Infinity"""
    timestamp: str
    device_id: str
    device_name: str
    temperature_c: float
    temperature_f: float
    humidity: float  # 0-100
    vpd: float
    ports: list[dict] = field(default_factory=list)
    external_sensors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "temperature_c": self.temperature_c,
            "temperature_f": self.temperature_f,
            "humidity": self.humidity,
            "vpd": self.vpd,
            "ports": self.ports or [],
            "external_sensors": self.external_sensors or [],
        }


def calculate_vpd(temp_c: float, humidity: float) -> float:
    """Calculate VPD using Magnus formula"""
    a = 17.27
    b = 237.7
    alpha = (a * temp_c) / (b + temp_c)
    svp = 0.6108 * math.exp(alpha)
    vpd = svp * (1 - humidity / 100.0)
    return round(vpd, 2)
