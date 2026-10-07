# Fabric Capacity Autoscaler (Activator + User Data Function)

Automatically scales a Microsoft Fabric capacity **up or down one SKU rung** when
capacity utilization crosses thresholds. A Fabric **Activator** watches the
*Fabric capacity overview events* stream in Real-Time hub and calls a Fabric
**User Data Function (UDF)**, which PATCHes the capacity SKU through Azure
Resource Manager.

```
Real-Time hub                     Activator                        User Data Function                 Azure ARM
Capacity.Summary (every 30s) -->  Rule: interactiveDelay% > 80  --> scale_fabric_capacity("up")   -->  PATCH sku F16 -> F32
                                  Rule: interactiveReject% < 30 --> scale_fabric_capacity("down") -->  PATCH sku F32 -> F16
```

## Repo layout

| Path | Purpose |
| --- | --- |
| `functions/function_app.py` | UDF code: `scale_fabric_capacity` and read-only `get_fabric_capacity` |
| `functions/requirements.txt` | Libraries to add to the UDF item |
| `scripts/setup-spn.ps1` | Creates a service principal with Contributor on **only** the capacity |
| `activator/rules.json` | Reference definition of the scale-up / scale-down rules |
| `eventstream/capacity_utilization.sql` | Optional Eventstream SQL that adds a `utilizationPct` column |
| `tests/test_function_app.py` | Local unit tests (no Azure calls) |

## Prerequisites

- A Fabric capacity (F SKU) in Azure, and rights to assign roles on it.
- A Fabric workspace on a capacity where you can create **User data functions** and **Activator** items.
- Azure CLI (`az login`) for the setup script.
- Fabric admin: *Capacity overview events* are visible to capacity admins in Real-Time hub.

## Steps

### 1. Create the service principal

```powershell
./scripts/setup-spn.ps1 -SubscriptionId <sub-id> -ResourceGroup <rg> -CapacityName <capacity>
```

This grants **Contributor scoped to the single capacity resource** and prints
`tenant_id`, `client_id`, `client_secret`. Save the secret somewhere safe.

### 2. Create the User Data Function item

1. In your workspace: **New item → User data functions** → name it `capacity-autoscaler`.
2. **Library management → Add from PyPI**: `azure-identity`, `requests` (see `functions/requirements.txt`).
3. Replace the generated `function_app.py` with `functions/function_app.py` from this repo.
4. Adjust `MIN_SKU`, `MAX_SKU`, `COOLDOWN_MINUTES` at the top of the file.
5. **Publish**.

### 3. Test the function manually

In the UDF portal, **Run** `get_fabric_capacity` with your values — you should get
`{"state": "Active", "sku": "F16", ...}`. Then run `scale_fabric_capacity` with
`direction = "up"` and confirm the SKU changes in the Azure portal. Run again
with `"down"` (wait out the cooldown or republish to reset it).

### 4. Connect the capacity events

1. **Real-Time hub → Fabric events → Fabric capacity overview events**.
2. Choose **Set alert** (goes straight to Activator), select your capacity and
   event type **Microsoft.Fabric.Capacity.Summary**.
   - *Optional:* choose **Create eventstream** instead, add a SQL operator with
     `eventstream/capacity_utilization.sql`, and route its output to an Activator
     destination if you prefer alerting on a plain `utilizationPct`.

### 5. Create the Activator rules

In the Activator item, create an object keyed on `capacityName` with these two rules
(full definition in `activator/rules.json`):

| Rule | Property | Condition | Action |
| --- | --- | --- | --- |
| **Scale UP** | `interactiveDelayThresholdPercentage` | > **80** sustained **10 min** | Run UDF `scale_fabric_capacity`, `direction = up` |
| **Scale DOWN** | `interactiveRejectionThresholdPercentage` | < **30** sustained **60 min** | Run UDF `scale_fabric_capacity`, `direction = down` |

For each rule: **Action → Run Fabric item → User data function** → pick
`capacity-autoscaler` → `scale_fabric_capacity` → fill in parameters
(`subscription_id`, `resource_group`, `capacity_name`, `direction`, `tenant_id`,
`client_id`, `client_secret`). Save and **Start** both rules.

**Why these thresholds?**
- `interactiveDelayThresholdPercentage` is the smoothed utilization of the next
  10 minutes; at 100% Fabric starts delaying interactive work, so scaling at 80%
  gets ahead of throttling.
- `interactiveRejectionThresholdPercentage` covers the next hour; staying below 30%
  for an hour is a safe signal. Halving the SKU roughly doubles the percentage
  (30% → ~60%), which stays under the 80% scale-up line — **keep
  `down threshold × 2 < up threshold`** to avoid flapping.
- Paused capacities emit no Summary events, so the rules never fire while paused.

### 6. Verify

- Activator **History** shows each firing and the UDF response
  (`scaled`, `noop` with a reason, or `error`).
- UDF **Logs** show `cap: state=..., sku=..., dir=...` lines.
- Azure portal → capacity → **Activity log** shows the SKU update by the SPN.

## How the function behaves

| Situation | Result |
| --- | --- |
| Active, not at limit | PATCH SKU one rung → `scaled` |
| Already at `MAX_SKU` (up) / `MIN_SKU` (down) | `noop` |
| SKU outside min/max range | moves toward the range only, never the wrong way |
| Scaled < `COOLDOWN_MINUTES` ago (warm instance) | `noop: cooldown` |
| Paused + down | `noop: paused` |
| Paused + up | resume, wait until `Active` (≤ 3 min), then scale |
| Resuming / Updating / other state | `noop: capacity busy` (next alert retries) |
| ARM / auth failure | `error` with HTTP status and message |

## Security notes

- The SPN only has Contributor on the one capacity resource.
- Activator stores action parameters in the rule definition, so anyone who can
  edit the Activator can read `client_secret`. Restrict workspace access, rotate
  the secret regularly, or adapt the UDF to fetch the secret from Azure Key Vault.
- Never commit real IDs or secrets; `activator/rules.json` uses placeholders.

## Limitations

- Cooldown is in-memory and resets on cold start. For a durable cooldown, persist
  the last scale time to a Fabric SQL database / Lakehouse table via a UDF connection.
- Scaling changes billing immediately; set `MAX_SKU` to what you are willing to pay for.

## Run tests locally

```powershell
pip install requests azure-identity
python -m unittest discover -s tests -v
```
