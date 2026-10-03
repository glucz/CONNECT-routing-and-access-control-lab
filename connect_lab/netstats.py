"""Read-only Linux interface counters; these include all host traffic, not just the lab."""
from pathlib import Path
import time


def snapshot():
    root = Path("/sys/class/net")
    values = {}
    if not root.is_dir():
        return {"available": False, "reason": "Linux sysfs unavailable", "time": time.time()}
    for interface in sorted(root.iterdir()):
        if interface.name == "lo":
            continue
        try:
            values[interface.name] = {name: int((interface / "statistics" / name).read_text())
                                      for name in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets")}
        except (OSError, ValueError):
            continue
    return {"available": bool(values), "interfaces": values, "time": time.time()}


def delta(before, after):
    result = {}
    for interface, start in before.get("interfaces", {}).items():
        end = after.get("interfaces", {}).get(interface)
        if end is not None:
            result[interface] = {key: end[key]-value if end[key] >= value else None
                                 for key, value in start.items()}
    return {"available": bool(result), "interfaces": result,
            "note": "All traffic on each interface, including SSH/control/NDP and background traffic; do not sum overlapping virtual/physical interfaces. Null means counter reset."}
