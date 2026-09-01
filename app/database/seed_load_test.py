"""Bulk-seeds high-volume rows into tables that seed_real_data.py does
not touch (users, journeys, enquiries, alerts, notifications,
notification_logs, news) so query-plan/EXPLAIN analysis and N+1
detection are done against realistic row counts instead of an
empty/near-empty table (which hides missing indexes - Postgres will
happily seq-scan 50 rows faster than it can use an index).

Not part of the app - a one-off analysis tool. Safe to re-run
(idempotent per row-count target: skips tables already at/above
target size).
"""
import random
import uuid
from datetime import datetime, timedelta, timezone

from app.database.session import SessionLocal
from app.enums.alert_type import AlertType
from app.enums.enquiry_category import EnquiryCategory
from app.enums.enquiry_status import EnquiryStatus
from app.enums.journey_status import JourneyStatus
from app.enums.notification_channel import NotificationChannel
from app.enums.notification_source import NotificationSource
from app.enums.notification_status import NotificationStatus
from app.enums.user_role import UserRole
from app.models.alert import Alert
from app.models.enquiry import Enquiry
from app.models.journey import Journey
from app.models.news import News
from app.models.notification import Notification
from app.models.notification_log import NotificationLog
from app.models.station import Station
from app.models.user_profile import UserProfile

N_USERS = 1000
N_JOURNEYS = 5000
N_ENQUIRIES = 1500
N_ALERTS = 2000
N_NOTIFICATIONS = 8000
N_NOTIFICATION_LOGS = 6000
N_NEWS = 300

def run():
    db = SessionLocal()
    try:
        station_ids = [s.id for s in db.query(Station.id).all()]
        if not station_ids:
            raise SystemExit("Run seed_real_data first (no stations found).")

        existing_users = db.query(UserProfile).count()
        if existing_users < N_USERS:
            to_add = N_USERS - existing_users
            users = []
            for i in range(to_add):
                uid = uuid.uuid4()
                users.append(UserProfile(
                    id=uid,
                    email=f"loadtest_user_{existing_users + i}@example.com",
                    full_name=f"Load Test User {existing_users + i}",
                    role=UserRole.PASSENGER,
                    is_active=True,
                ))
            db.add_all(users)
            db.commit()
            print(f"users: +{to_add}")
        user_ids = [u.id for u in db.query(UserProfile.id).all()]
        admin = db.query(UserProfile).filter(UserProfile.role == UserRole.ADMIN).first()
        if not admin:
            some_user = db.query(UserProfile).first()
            some_user.role = UserRole.ADMIN
            some_user.email = "admin@example.com"
            db.commit()
            print(f"promoted {some_user.email} to admin")

        now = datetime.now(timezone.utc)

        if db.query(Journey).count() < N_JOURNEYS:
            batch = []
            for i in range(N_JOURNEYS):
                src, dst = random.sample(station_ids, 2)
                checkin = now - timedelta(minutes=random.randint(1, 60 * 24 * 30))
                status = random.choice([JourneyStatus.COMPLETED] * 9 + [JourneyStatus.ACTIVE])
                j = Journey(
                    user_id=random.choice(user_ids),
                    source_station_id=src,
                    destination_station_id=dst,
                    checkin_time=checkin,
                    checkout_time=checkin + timedelta(minutes=random.randint(5, 90)) if status == JourneyStatus.COMPLETED else None,
                    fare=round(random.uniform(10, 80), 2) if status == JourneyStatus.COMPLETED else None,
                    status=status,
                )
                batch.append(j)
                if len(batch) >= 1000:
                    db.add_all(batch)
                    db.commit()
                    batch = []
            if batch:
                db.add_all(batch)
                db.commit()
            print(f"journeys: {N_JOURNEYS}")

        if db.query(Enquiry).count() < N_ENQUIRIES:
            batch = []
            for i in range(N_ENQUIRIES):
                status = random.choice(list(EnquiryStatus))
                created = now - timedelta(minutes=random.randint(1, 60 * 24 * 60))
                e = Enquiry(
                    user_id=random.choice(user_ids),
                    subject=f"Enquiry subject {i}",
                    category=random.choice(list(EnquiryCategory)),
                    message="Load test enquiry message body.",
                    status=status,
                    resolved_by=random.choice(user_ids) if status == EnquiryStatus.RESOLVED else None,
                    resolved_at=created + timedelta(hours=2) if status == EnquiryStatus.RESOLVED else None,
                )
                e.created_at = created
                batch.append(e)
                if len(batch) >= 1000:
                    db.add_all(batch)
                    db.commit()
                    batch = []
            if batch:
                db.add_all(batch)
                db.commit()
            print(f"enquiries: {N_ENQUIRIES}")

        if db.query(Alert).count() < N_ALERTS:
            batch = []
            alert_ids_created = []
            for i in range(N_ALERTS):
                resolved = random.random() < 0.7
                created = now - timedelta(minutes=random.randint(1, 60 * 24 * 90))
                a = Alert(
                    station_id=random.choice(station_ids),
                    alert_type=random.choice(list(AlertType)),
                    message=f"Load test alert {i}",
                    created_by=random.choice(user_ids),
                    is_resolved=resolved,
                    resolved_at=created + timedelta(minutes=30) if resolved else None,
                )
                a.created_at = created
                batch.append(a)
                if len(batch) >= 1000:
                    db.add_all(batch)
                    db.commit()
                    batch = []
            if batch:
                db.add_all(batch)
                db.commit()
            print(f"alerts: {N_ALERTS}")

        alert_ids = [a.id for a in db.query(Alert.id).all()]

        if db.query(Notification).count() < N_NOTIFICATIONS:
            batch = []
            for i in range(N_NOTIFICATIONS):
                created = now - timedelta(minutes=random.randint(1, 60 * 24 * 14))
                broadcast = random.random() < 0.4
                n = Notification(
                    user_id=None if broadcast else random.choice(user_ids),
                    source=random.choice(list(NotificationSource)),
                    title=f"Load test notification {i}",
                    message="Load test notification body.",
                    related_alert_id=random.choice(alert_ids) if alert_ids and random.random() < 0.3 else None,
                    state=random.choice(["West Bengal", "Delhi", "Karnataka", None]),
                    is_read=random.random() < 0.5,
                )
                n.created_at = created
                batch.append(n)
                if len(batch) >= 1000:
                    db.add_all(batch)
                    db.commit()
                    batch = []
            if batch:
                db.add_all(batch)
                db.commit()
            print(f"notifications: {N_NOTIFICATIONS}")

        if db.query(NotificationLog).count() < N_NOTIFICATION_LOGS and alert_ids:
            batch = []
            for i in range(N_NOTIFICATION_LOGS):
                sent = random.random() < 0.85
                created = now - timedelta(minutes=random.randint(1, 60 * 24 * 90))
                log = NotificationLog(
                    alert_id=random.choice(alert_ids),
                    channel=random.choice(list(NotificationChannel)),
                    recipient=f"user{i}@example.com",
                    status=NotificationStatus.SENT if sent else NotificationStatus.FAILED,
                    error_message=None if sent else "simulated failure",
                    sent_at=created if sent else None,
                )
                log.created_at = created
                batch.append(log)
                if len(batch) >= 1000:
                    db.add_all(batch)
                    db.commit()
                    batch = []
            if batch:
                db.add_all(batch)
                db.commit()
            print(f"notification_logs: {N_NOTIFICATION_LOGS}")

        if db.query(News).count() < N_NEWS:
            batch = []
            for i in range(N_NEWS):
                created = now - timedelta(minutes=random.randint(1, 60 * 24 * 60))
                news = News(
                    title=f"Load test news {i}",
                    content="Load test news content.",
                    created_by=random.choice(user_ids),
                    is_active=random.random() < 0.9,
                )
                news.created_at = created
                batch.append(news)
                if len(batch) >= 1000:
                    db.add_all(batch)
                    db.commit()
                    batch = []
            if batch:
                db.add_all(batch)
                db.commit()
            print(f"news: {N_NEWS}")

        print("Load-test seeding complete.")
    finally:
        db.close()

if __name__ == "__main__":
    run()
