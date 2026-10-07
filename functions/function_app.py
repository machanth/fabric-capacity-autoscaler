import time
import logging
from datetime import datetime, timezone

import requests
import fabric.functions as fn
from azure.identity import ClientSecretCredential

udf = fn.UserDataFunctions()

ARM_API = "2023-11-01"
ARM_BASE = "https://management.azure.com"

# Ordered SKU ladder. Add/remove rungs to match what you actually use.
SKU_LADDER = ["F2", "F4", "F8", "F16", "F32", "F64",
              "F128", "F256", "F512", "F1024", "F2048"]

# Floors and ceilings — protect yourself from runaway scaling.
MIN_SKU = "F8"
MAX_SKU = "F256"

# Minimum minutes between scale operations on the same capacity.
COOLDOWN_MINUTES = 15

# How long to wait for a resumed capacity to become Active before scaling.
RESUME_TIMEOUT_SECONDS = 180

# In-memory cooldown cache. Survives only within a warm function instance.
# For durable cooldown, swap this for a Lakehouse/SQL table read/write.
_last_scale: dict[str, datetime] = {}


def _next_sku(current: str, direction: str) -> str | None:
    """Return next SKU up or down the ladder, respecting MIN/MAX. None if no move."""
    if current not in SKU_LADDER:
        return None
    idx = SKU_LADDER.index(current)
    min_idx = SKU_LADDER.index(MIN_SKU)
    max_idx = SKU_LADDER.index(MAX_SKU)

    if direction == "up":
        target_idx = max(min(idx + 1, max_idx), min_idx)
        if target_idx <= idx:
            return None  # at/above ceiling — never move down on an "up" request
    elif direction == "down":
        target_idx = min(max(idx - 1, min_idx), max_idx)
        if target_idx >= idx:
            return None  # at/below floor — never move up on a "down" request
    else:
        return None
    return SKU_LADDER[target_idx]


def _in_cooldown(capacity_name: str) -> bool:
    last = _last_scale.get(capacity_name)
    if not last:
        return False
    age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return age_min < COOLDOWN_MINUTES


def _capacity_url(subscription_id: str, resource_group: str, capacity_name: str) -> str:
    return (f"{ARM_BASE}/subscriptions/{subscription_id}"
            f"/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Fabric/capacities/{capacity_name}")


def _headers(tenant_id: str, client_id: str, client_secret: str) -> dict:
    cred = ClientSecretCredential(tenant_id, client_id, client_secret)
    token = cred.get_token(f"{ARM_BASE}/.default").token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get_capacity(base: str, headers: dict) -> dict:
    r = requests.get(f"{base}?api-version={ARM_API}", headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def _wait_until_active(base: str, headers: dict) -> str:
    deadline = time.monotonic() + RESUME_TIMEOUT_SECONDS
    state = "Unknown"
    while time.monotonic() < deadline:
        state = _get_capacity(base, headers)["properties"]["state"]
        if state == "Active":
            return state
        time.sleep(10)
    return state


@udf.function()
def scale_fabric_capacity(
    subscription_id: str,
    resource_group: str,
    capacity_name: str,
    direction: str,                 # "up" or "down"
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    Scale a Fabric capacity one rung up or down.
    Idempotent: returns 'noop' if already at floor/ceiling, in cooldown,
    busy with another operation, or paused-and-direction-down.
    """
    direction = (direction or "").strip().lower()
    if direction not in ("up", "down"):
        return {"status": "error", "reason": f"invalid direction '{direction}'"}

    if _in_cooldown(capacity_name):
        return {"status": "noop", "reason": "cooldown", "capacity": capacity_name}

    try:
        headers = _headers(tenant_id, client_id, client_secret)
        base = _capacity_url(subscription_id, resource_group, capacity_name)

        info = _get_capacity(base, headers)
        state = info["properties"]["state"]
        current_sku = info["sku"]["name"]
        logging.info(f"{capacity_name}: state={state}, sku={current_sku}, dir={direction}")

        if state == "Paused":
            if direction == "down":
                return {"status": "noop", "reason": "paused", "capacity": capacity_name}
            rs = requests.post(f"{base}/resume?api-version={ARM_API}",
                               headers=headers, timeout=60)
            rs.raise_for_status()
            state = _wait_until_active(base, headers)
            if state != "Active":
                return {"status": "error", "reason": f"resume timed out (state={state})",
                        "capacity": capacity_name}
        elif state != "Active":
            # Resuming, Scaling, Updating, Pausing, ... — let the next alert retry.
            return {"status": "noop", "reason": f"capacity busy (state={state})",
                    "capacity": capacity_name, "sku": current_sku}

        target_sku = _next_sku(current_sku, direction)
        if not target_sku:
            return {"status": "noop",
                    "reason": f"already at {'ceiling' if direction == 'up' else 'floor'}",
                    "capacity": capacity_name, "sku": current_sku}

        body = {"sku": {"name": target_sku, "tier": "Fabric"}}
        rp = requests.patch(f"{base}?api-version={ARM_API}",
                            headers=headers, json=body, timeout=60)
        rp.raise_for_status()

        _last_scale[capacity_name] = datetime.now(timezone.utc)
        logging.info(f"{capacity_name}: scaled {current_sku} -> {target_sku}")

        return {"status": "scaled",
                "capacity": capacity_name,
                "from_sku": current_sku,
                "to_sku": target_sku,
                "direction": direction}
    except requests.HTTPError as e:
        resp = e.response
        return {"status": "error", "capacity": capacity_name,
                "http_status": resp.status_code if resp is not None else None,
                "reason": (resp.text[:500] if resp is not None else str(e))}
    except Exception as e:  # surface failures to Activator instead of a bare 500
        logging.exception("scale_fabric_capacity failed")
        return {"status": "error", "capacity": capacity_name, "reason": str(e)}


@udf.function()
def get_fabric_capacity(
    subscription_id: str,
    resource_group: str,
    capacity_name: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Read-only helper to test the SPN/permissions: returns state and SKU."""
    headers = _headers(tenant_id, client_id, client_secret)
    info = _get_capacity(_capacity_url(subscription_id, resource_group, capacity_name), headers)
    return {"capacity": capacity_name,
            "state": info["properties"]["state"],
            "sku": info["sku"]["name"],
            "cooldown": _in_cooldown(capacity_name)}
