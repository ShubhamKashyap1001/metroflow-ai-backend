"""Generates a demo metro network (lines, stations, trains) used by
`app/database/seed.py`. Centralized here so the simulator and the
seed script share the same station list/ids.
"""

DEMO_STATIONS = [
    {"station_code": "STN01", "station_name": "Central Station", "city": "Metro City",
     "latitude": 25.6127, "longitude": 85.1450, "is_interchange": True, "capacity": 8000},
    {"station_code": "STN02", "station_name": "Riverside", "city": "Metro City",
     "latitude": 25.6201, "longitude": 85.1520, "is_interchange": False, "capacity": 4000},
    {"station_code": "STN03", "station_name": "Tech Park", "city": "Metro City",
     "latitude": 25.6305, "longitude": 85.1610, "is_interchange": False, "capacity": 6000},
    {"station_code": "STN04", "station_name": "Old Town", "city": "Metro City",
     "latitude": 25.6050, "longitude": 85.1390, "is_interchange": False, "capacity": 3500},
    {"station_code": "STN05", "station_name": "University", "city": "Metro City",
     "latitude": 25.6400, "longitude": 85.1700, "is_interchange": False, "capacity": 5000},
    {"station_code": "STN06", "station_name": "Airport Link", "city": "Metro City",
     "latitude": 25.6480, "longitude": 85.1800, "is_interchange": True, "capacity": 7000},
]

DEMO_LINE = {
    "line_code": "LINE-1",
    "line_name": "Blue Line",
    "color": "#1E88E5",
}

DEMO_TRAINS = [
    {"train_number": "TRN-101", "capacity": 1200},
    {"train_number": "TRN-102", "capacity": 1200},
    {"train_number": "TRN-103", "capacity": 900},
]
