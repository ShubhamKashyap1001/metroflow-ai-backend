"""India state <-> metro city mapping.

MetroFlow's seeded station data (see app/database/seed_real_data.py)
is real, city-by-city data across 12 Indian metro cities. There's no
`state` column on any table - state selection in the UI is a filter
over the cities that already exist, resolved here so every service
(stations, trains, crowd, schedules, alerts, analytics) filters the
same way.
"""

STATE_CITY_MAP: dict[str, list[str]] = {
    "Delhi": ["Delhi"],
    "Maharashtra": ["Mumbai", "Pune"],
    "Karnataka": ["Bengaluru"],
    "Telangana": ["Hyderabad"],
    "Tamil Nadu": ["Chennai"],
    "West Bengal": ["Kolkata"],
    "Gujarat": ["Ahmedabad"],
    "Rajasthan": ["Jaipur"],
    "Uttar Pradesh": ["Lucknow"],
    "Madhya Pradesh": ["Bhopal"],
    "Kerala": ["Kochi"],
}

MIN_STATIONS_FOR_SUFFICIENT_DATA = 3

def cities_for_state(state: str | None) -> list[str] | None:
    """Cities belonging to `state`, or None if no filter was requested.

    Also accepts a raw city name (e.g. `?state=Mumbai`) so callers that
    already know the city don't need to look up its state first.
    """
    if not state:
        return None

    state = state.strip()
    if state in STATE_CITY_MAP:
        return STATE_CITY_MAP[state]

    for cities in STATE_CITY_MAP.values():
        if state in cities:
            return [state]

    return [state]

def state_for_city(city: str) -> str | None:
    for state, cities in STATE_CITY_MAP.items():
        if city in cities:
            return state
    return None
