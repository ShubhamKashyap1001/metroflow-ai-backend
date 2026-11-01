import os
import sys
from urllib.parse import urljoin

import httpx

def main() -> int:
    base_url = os.environ.get("SMOKE_TEST_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("SMOKE_TEST_ACCESS_TOKEN", "").strip()
    if not base_url or not token:
        print("Set SMOKE_TEST_BASE_URL and SMOKE_TEST_ACCESS_TOKEN.")
        return 2

    headers = {"Authorization": f"Bearer {token}"}
    checks = [
        ("health", "/health/", False),
        ("authenticated profile", "/api/v1/auth/me", True),
        ("stations", "/api/v1/stations/?limit=1", False),
        ("live trains", "/api/v1/trains/live", True),
        ("crowd dashboard", "/api/v1/crowd/dashboard", True),
        ("notification preferences", "/api/v1/notifications/preferences", True),
        ("traffic report", "/api/v1/analytics/traffic-report?hours=24", True),
        ("system status", "/api/v1/admin/system-status", True),
        ("simulator controls", "/api/v1/admin/simulator", True),
    ]
    failures = 0
    timeout = httpx.Timeout(15.0, connect=8.0)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for name, path, authenticated in checks:
            try:
                response = client.get(urljoin(f"{base_url}/", path.lstrip("/")), headers=headers if authenticated else {})
                if response.is_success:
                    print(f"PASS {name}: HTTP {response.status_code}")
                else:
                    failures += 1
                    detail = response.text.replace("\n", " ")[:240]
                    print(f"FAIL {name}: HTTP {response.status_code} {detail}")
            except httpx.HTTPError as exc:
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")

    print(f"Staging smoke checks: {len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
