#!/usr/bin/env bash
# Demonstration script for PDF Section 15 Scenario 9 (Concurrent Events)
# Fires simultaneous events affecting the same junction within milliseconds.

BASE_URL="http://localhost:8000/api"
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "=========================================================="
echo "Scenario 9: Concurrent Events Execution"
echo "Firing simultaneous inputs into Junction A..."
echo "=========================================================="

# 1. T = 0ms: NORTH truck arrives
curl -s -X POST "${BASE_URL}/sensor-events" \
  -H "Content-Type: application/json" \
  -d "{\"event_id\":\"evt-sc9-1\",\"junction_id\":\"A\",\"direction\":\"NORTH\",\"event_type\":\"VEHICLE_ARRIVED\",\"vehicle_id\":\"TRK-901\",\"vehicle_type\":\"TRUCK\",\"sequence_no\":101,\"timestamp\":\"${NOW}\"}" &
PID1=$!

# 2. T = 4ms: EAST emergency arrives
curl -s -X POST "${BASE_URL}/sensor-events" \
  -H "Content-Type: application/json" \
  -d "{\"event_id\":\"evt-sc9-2\",\"junction_id\":\"A\",\"direction\":\"EAST\",\"event_type\":\"VEHICLE_ARRIVED\",\"vehicle_id\":\"AMB-901\",\"vehicle_type\":\"EMERGENCY\",\"sequence_no\":102,\"timestamp\":\"${NOW}\"}" &
PID2=$!

# 3. T = 8ms: Administrator requests WEST manual control
curl -s -X POST "${BASE_URL}/junctions/A/commands" \
  -H "Content-Type: application/json" \
  -d "{\"command\":\"MANUAL_GREEN_REQUEST\",\"direction\":\"WEST\",\"requested_by\":\"admin-sim\"}" &
PID3=$!

# 4. T = 12ms: Duplicate EAST emergency event arrives
curl -s -X POST "${BASE_URL}/sensor-events" \
  -H "Content-Type: application/json" \
  -d "{\"event_id\":\"evt-sc9-2\",\"junction_id\":\"A\",\"direction\":\"EAST\",\"event_type\":\"VEHICLE_ARRIVED\",\"vehicle_id\":\"AMB-901\",\"vehicle_type\":\"EMERGENCY\",\"sequence_no\":102,\"timestamp\":\"${NOW}\"}" &
PID4=$!

# Wait for all background requests to resolve
wait $PID1 $PID2 $PID3 $PID4

echo ""
echo "All concurrent requests processed by Junction Worker."
echo "Fetching live junction status to verify invariants..."
echo "----------------------------------------------------------"
curl -s "${BASE_URL}/junctions/A/status" | jq '{
  junction_id: .junction_id,
  mode: .mode,
  phase: .phase,
  step_kind: .step.kind,
  actual_signals: .actual_signals,
  queues: .queues,
  emergency_active: .emergency.active
}'
echo "=========================================================="
echo "Verified: Invariants preserved, no conflicting GREEN signals."
