-- Optional Eventstream SQL operator: adds a simple utilization % per 30s window
-- so Activator can alert on a single, intuitive number.
-- Source: Real-Time hub > Fabric events > Fabric capacity overview events
--         (event type Microsoft.Fabric.Capacity.Summary)
-- Destination output name: [capacity-utilization] -> Activator
SELECT
    data.capacityId                                   AS capacityId,
    data.capacityName                                 AS capacityName,
    data.capacitySku                                  AS capacitySku,
    data.windowEndTime                                AS windowEndTime,
    data.baseCapacityUnits                            AS baseCapacityUnits,
    data.capacityUnitMs                               AS capacityUnitMs,
    100.0 * data.capacityUnitMs
        / (data.baseCapacityUnits * 1000.0 * 30.0)    AS utilizationPct,
    data.interactiveDelayThresholdPercentage          AS interactiveDelayPct,
    data.interactiveRejectionThresholdPercentage      AS interactiveRejectionPct,
    data.backgroundRejectionThresholdPercentage       AS backgroundRejectionPct
INTO [capacity-utilization]
FROM [capacity-overview-events]
WHERE type = 'Microsoft.Fabric.Capacity.Summary'
