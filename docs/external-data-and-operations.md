# External data and operations

MetroFlow keeps the existing passenger dataset and trained model artifacts. External data is additive and is stored with event identifiers so retries do not duplicate crowd history. The sample simulator remains enabled until an operator disables it after a real source has been verified.

## Crowd counter, ticketing, or sensor data

Set `SENSOR_INGEST_API_KEY` to a long random secret. Send batches of at most 50 normalized station counts to `POST /api/v1/integrations/crowd`:

```http
X-Feed-Key: <SENSOR_INGEST_API_KEY>
X-Feed-Source: kmrl-gate-counter
Content-Type: application/json
```

```json
{
  "records": [
    {
      "event_id": "source-unique-event-id",
      "station_code": "A1",
      "current_count": 148,
      "observed_at": "2026-10-31T08:15:00+05:30"
    }
  ]
}
```

`station_code` must already exist in the station table. Timestamps must include a timezone. Repeated `event_id` values from the same source are acknowledged as duplicates without adding a second crowd log. Old readings remain in history but cannot overwrite a newer live station count.

## Train telemetry

Set `METRO_TRAIN_INGEST_API_KEY` and post normalized vehicle records to `POST /api/v1/integrations/trains`. The request uses the same `X-Feed-Key` and `X-Feed-Source` headers and accepts up to 100 rows with `event_id`, `train_number`, `station_code`, optional `next_station_code`, `progress_ratio` from 0 to 1, `status`, `delay_minutes`, and timezone-aware `observed_at`. Train numbers and station codes must match existing records. Updates are deduplicated, stale positions are ignored, and accepted positions publish through the existing WebSocket event.

These are authenticated ingestion contracts for a counter, ticketing gateway, or operator adapter. They do not scrape or decode an operator GTFS-Realtime feed. A deployment that receives GTFS-Realtime protobuf must run an authorized translator that maps its trip/vehicle/station IDs to MetroFlow's train numbers and station codes before posting. Do not disable the simulator until upstream data has been observed and verified.

## Schedule optimization

Admin and operator roles can call `POST /api/v1/schedules/optimization/preview?state=Kerala`. The preview uses the existing trained frequency model and current peak-hour schedules. It reports the saved hold-out evaluation separately from current occupancy context. It does not apply changes. Operators apply one proposed interval at a time through the existing frequency adjustment workflow.

Delay reduction is reported as pending until real post-adjustment service observations are available; a recommendation or model test score is not evidence that an operational target was achieved.

## Alert delivery bridges

`MOBILE_PUSH_WEBHOOK_URL` and `MOBILE_PUSH_WEBHOOK_TOKEN` forward alert creation and resolution events to a deployment-owned push gateway. `PUBLIC_ADDRESS_WEBHOOK_URL` and `PUBLIC_ADDRESS_WEBHOOK_TOKEN` forward emergency creation and resolution events to a deployment-owned station announcement gateway. Endpoints must use HTTPS outside local debug. Payloads include the event name, alert id/type, station id/code/name, message, and creation or resolution time; a successful response must be a 2xx. MetroFlow does not provide the push subscription service or the PA vendor's command protocol, so configure a gateway that owns those functions and test its audience, station routing, and acknowledgement behavior before enabling it.

The metadata endpoint `/api/v1/meta/data-sources` exposes only whether each integration is configured; it never returns API keys or webhook tokens.
