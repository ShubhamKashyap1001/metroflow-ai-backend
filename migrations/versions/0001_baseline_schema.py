from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_index_names(inspector: sa.engine.reflection.Inspector, table_name: str) -> set[str]:
    """Names of indexes that already exist on `table_name`, freshly
    reflected from the connected database (never cached across calls),
    so a resumed/duplicated `alembic upgrade head` run always checks
    the database's actual current state rather than a stale snapshot
    taken before this revision started creating things."""
    return {ix["name"] for ix in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- Tables (created only if missing - safe no-op on any database
    # that already has them) ---

    if "ai_models" not in inspector.get_table_names():
        op.create_table('ai_models',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('model_name', sa.String(length=100), nullable=False),
            sa.Column('version', sa.String(length=30), nullable=False),
            sa.Column('accuracy', sa.Float(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.PrimaryKeyConstraint('id')
            )

    if "metro_lines" not in inspector.get_table_names():
        op.create_table('metro_lines',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('line_code', sa.String(length=20), nullable=False),
            sa.Column('line_name', sa.String(length=100), nullable=False),
            sa.Column('color', sa.String(length=20), nullable=False),
            sa.Column('status', sa.Enum('ACTIVE', 'SUSPENDED', 'MAINTENANCE', name='linestatus'), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('line_code')
            )

    if "routes" not in inspector.get_table_names():
        op.create_table('routes',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('route_name', sa.String(length=100), nullable=False),
            sa.Column('start_station', sa.String(length=100), nullable=False),
            sa.Column('end_station', sa.String(length=100), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('route_name')
            )

    if "stations" not in inspector.get_table_names():
        op.create_table('stations',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('station_code', sa.String(length=20), nullable=False),
            sa.Column('station_name', sa.String(length=120), nullable=False),
            sa.Column('city', sa.String(length=100), nullable=False),
            sa.Column('latitude', sa.Float(), nullable=False),
            sa.Column('longitude', sa.Float(), nullable=False),
            sa.Column('is_interchange', sa.Boolean(), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.Column('capacity', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('station_code')
            )

    if "trains" not in inspector.get_table_names():
        op.create_table('trains',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('train_number', sa.String(length=30), nullable=False),
            sa.Column('capacity', sa.Integer(), nullable=False),
            sa.Column('commissioned_date', sa.Date(), nullable=True),
            sa.Column('status', sa.Enum('ACTIVE', 'IN_SERVICE', 'DELAYED', 'MAINTENANCE', 'OUT_OF_SERVICE', name='trainstatus'), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('train_number')
            )

    if "user_profiles" not in inspector.get_table_names():
        op.create_table('user_profiles',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('email', sa.String(length=255), nullable=True),
            sa.Column('full_name', sa.String(length=120), nullable=False),
            sa.Column('username', sa.String(length=50), nullable=True),
            sa.Column('phone', sa.String(length=20), nullable=True),
            sa.Column('avatar_url', sa.String(length=255), nullable=True),
            sa.Column('role', sa.Enum('ADMIN', 'OPERATOR', 'PASSENGER', name='userrole'), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('email')
            )

    if "alerts" not in inspector.get_table_names():
        op.create_table('alerts',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('station_id', sa.Integer(), nullable=False),
            sa.Column('alert_type', sa.Enum('OVERCROWDING', 'DELAY', 'EMERGENCY', 'MAINTENANCE', 'INFO', name='alerttype'), nullable=False),
            sa.Column('message', sa.String(length=500), nullable=False),
            sa.Column('created_by', sa.UUID(), nullable=True),
            sa.Column('is_resolved', sa.Boolean(), nullable=False),
            sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('available_until', sa.DateTime(timezone=True), nullable=True),
            sa.Column('notify_email', sa.Boolean(), nullable=False),
            sa.Column('notify_sms', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['created_by'], ['user_profiles.id'], ),
            sa.ForeignKeyConstraint(['station_id'], ['stations.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "crowd_logs" not in inspector.get_table_names():
        op.create_table('crowd_logs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('station_id', sa.Integer(), nullable=False),
            sa.Column('current_count', sa.Integer(), nullable=False),
            sa.Column('crowd_level', sa.Enum('LOW', 'MODERATE', 'HIGH', 'CRITICAL', name='crowdlevel'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['station_id'], ['stations.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "crowd_logs_hourly" not in inspector.get_table_names():
        op.create_table('crowd_logs_hourly',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('station_id', sa.Integer(), nullable=False),
            sa.Column('hour_bucket', sa.DateTime(timezone=True), nullable=False),
            sa.Column('avg_count', sa.Float(), nullable=False),
            sa.Column('max_count', sa.Integer(), nullable=False),
            sa.Column('min_count', sa.Integer(), nullable=False),
            sa.Column('sample_count', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['station_id'], ['stations.id'], ),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('station_id', 'hour_bucket', name='uq_crowd_logs_hourly_station_hour')
            )

    if "enquiries" not in inspector.get_table_names():
        op.create_table('enquiries',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.UUID(), nullable=False),
            sa.Column('subject', sa.String(length=200), nullable=False),
            sa.Column('category', sa.Enum('GENERAL', 'TICKETING', 'LOST_AND_FOUND', 'SAFETY', 'TECHNICAL', 'COMPLAINT', 'SUGGESTION', 'OTHER', name='enquirycategory'), nullable=False),
            sa.Column('message', sa.String(length=1000), nullable=False),
            sa.Column('status', sa.Enum('OPEN', 'IN_PROGRESS', 'RESOLVED', name='enquirystatus'), nullable=False),
            sa.Column('admin_reply', sa.String(length=1000), nullable=True),
            sa.Column('resolved_by', sa.UUID(), nullable=True),
            sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['resolved_by'], ['user_profiles.id'], ),
            sa.ForeignKeyConstraint(['user_id'], ['user_profiles.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "journeys" not in inspector.get_table_names():
        op.create_table('journeys',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('user_id', sa.UUID(), nullable=False),
            sa.Column('source_station_id', sa.Integer(), nullable=False),
            sa.Column('destination_station_id', sa.Integer(), nullable=False),
            sa.Column('checkin_time', sa.DateTime(timezone=True), nullable=False),
            sa.Column('checkout_time', sa.DateTime(timezone=True), nullable=True),
            sa.Column('fare', sa.Float(), nullable=False),
            sa.Column('status', sa.Enum('ACTIVE', 'COMPLETED', 'CANCELLED', name='journeystatus'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['destination_station_id'], ['stations.id'], ),
            sa.ForeignKeyConstraint(['source_station_id'], ['stations.id'], ),
            sa.ForeignKeyConstraint(['user_id'], ['user_profiles.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "line_stations" not in inspector.get_table_names():
        op.create_table('line_stations',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('line_id', sa.Integer(), nullable=False),
            sa.Column('station_id', sa.Integer(), nullable=False),
            sa.Column('station_order', sa.Integer(), nullable=False),
            sa.Column('distance_from_previous', sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(['line_id'], ['metro_lines.id'], ),
            sa.ForeignKeyConstraint(['station_id'], ['stations.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "news" not in inspector.get_table_names():
        op.create_table('news',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(length=200), nullable=False),
            sa.Column('content', sa.String(length=2000), nullable=False),
            sa.Column('created_by', sa.UUID(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['created_by'], ['user_profiles.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "predictions" not in inspector.get_table_names():
        op.create_table('predictions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('station_id', sa.Integer(), nullable=False),
            sa.Column('predicted_count', sa.Integer(), nullable=True),
            sa.Column('confidence', sa.Float(), nullable=False),
            sa.Column('prediction_type', sa.Enum('CROWD', 'DEMAND', 'DELAY', 'FREQUENCY', name='predictiontype'), nullable=False),
            sa.Column('predicted_value', sa.Float(), nullable=False),
            sa.Column('target_datetime', sa.DateTime(timezone=True), nullable=True),
            sa.Column('model_version', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['station_id'], ['stations.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "station_crowd_state" not in inspector.get_table_names():
        op.create_table('station_crowd_state',
            sa.Column('station_id', sa.Integer(), nullable=False),
            sa.Column('current_count', sa.Integer(), nullable=False),
            sa.Column('crowd_level', sa.Enum('LOW', 'MODERATE', 'HIGH', 'CRITICAL', name='crowdlevel'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['station_id'], ['stations.id'], ),
            sa.PrimaryKeyConstraint('station_id')
            )

    if "train_locations" not in inspector.get_table_names():
        op.create_table('train_locations',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('train_id', sa.Integer(), nullable=False),
            sa.Column('station_id', sa.Integer(), nullable=False),
            sa.Column('next_station_id', sa.Integer(), nullable=True),
            sa.Column('progress_ratio', sa.Float(), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['next_station_id'], ['stations.id'], ),
            sa.ForeignKeyConstraint(['station_id'], ['stations.id'], ),
            sa.ForeignKeyConstraint(['train_id'], ['trains.id'], ),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('train_id', name='uq_train_locations_train_id')
            )

    if "train_schedule_history" not in inspector.get_table_names():
        op.create_table('train_schedule_history',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('trip_id', sa.String(length=32), nullable=False),
            sa.Column('train_id', sa.Integer(), nullable=False),
            sa.Column('station_id', sa.Integer(), nullable=False),
            sa.Column('service_date', sa.Date(), nullable=False),
            sa.Column('station_sequence', sa.Integer(), nullable=False),
            sa.Column('scheduled_arrival', sa.Time(), nullable=False),
            sa.Column('scheduled_departure', sa.Time(), nullable=False),
            sa.Column('actual_arrival', sa.Time(), nullable=True),
            sa.Column('actual_departure', sa.Time(), nullable=True),
            sa.Column('delay_arrival_min', sa.Float(), nullable=False),
            sa.Column('delay_departure_min', sa.Float(), nullable=False),
            sa.Column('passenger_density', sa.String(length=16), nullable=True),
            sa.Column('weather', sa.String(length=32), nullable=True),
            sa.Column('delay_reason', sa.String(length=64), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['station_id'], ['stations.id'], ),
            sa.ForeignKeyConstraint(['train_id'], ['trains.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "train_schedules" not in inspector.get_table_names():
        op.create_table('train_schedules',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('train_id', sa.Integer(), nullable=False),
            sa.Column('station_id', sa.Integer(), nullable=False),
            sa.Column('arrival_time', sa.Time(), nullable=False),
            sa.Column('departure_time', sa.Time(), nullable=False),
            sa.Column('platform_number', sa.Integer(), nullable=False),
            sa.Column('station_sequence', sa.Integer(), nullable=True),
            sa.Column('day_type', sa.Enum('WEEKDAY', 'WEEKEND', 'HOLIDAY', name='daytype'), nullable=False),
            sa.Column('is_peak_hour', sa.Boolean(), nullable=False),
            sa.Column('frequency_minutes', sa.Integer(), nullable=False),
            sa.Column('status', sa.Enum('ON_TIME', 'DELAYED', 'CANCELLED', 'COMPLETED', name='schedulestatus'), nullable=False),
            sa.Column('delay_minutes', sa.Integer(), nullable=False),
            sa.Column('actual_arrival_time', sa.Time(), nullable=True),
            sa.Column('actual_departure_time', sa.Time(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['station_id'], ['stations.id'], ),
            sa.ForeignKeyConstraint(['train_id'], ['trains.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "notification_dispatch_jobs" not in inspector.get_table_names():
        op.create_table('notification_dispatch_jobs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('kind', sa.Enum('ALERT_CREATED', 'ALERT_RESOLVED', name='notificationdispatchkind'), nullable=False),
            sa.Column('alert_id', sa.Integer(), nullable=False),
            sa.Column('actor_id', sa.String(length=64), nullable=True),
            sa.Column('notify_email', sa.Boolean(), nullable=False),
            sa.Column('notify_sms', sa.Boolean(), nullable=False),
            sa.Column('status', sa.Enum('QUEUED', 'IN_PROGRESS', 'DONE', 'FAILED', name='notificationdispatchstatus'), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False),
            sa.Column('last_error', sa.String(length=500), nullable=True),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['alert_id'], ['alerts.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "notifications" not in inspector.get_table_names():
        op.create_table('notifications',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.UUID(), nullable=True),
            sa.Column('source', sa.Enum('EMAIL', 'OPERATOR', 'SYSTEM', 'SYSTEM_FAILURE', name='notificationsource'), nullable=False),
            sa.Column('title', sa.String(length=200), nullable=False),
            sa.Column('message', sa.String(length=1000), nullable=False),
            sa.Column('related_alert_id', sa.Integer(), nullable=True),
            sa.Column('state', sa.String(length=50), nullable=True),
            sa.Column('is_read', sa.Boolean(), nullable=False),
            sa.Column('binned_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['related_alert_id'], ['alerts.id'], ),
            sa.ForeignKeyConstraint(['user_id'], ['user_profiles.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "notification_logs" not in inspector.get_table_names():
        op.create_table('notification_logs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('alert_id', sa.Integer(), nullable=False),
            sa.Column('job_id', sa.Integer(), nullable=True),
            sa.Column('channel', sa.Enum('EMAIL', 'SMS', name='notificationchannel'), nullable=False),
            sa.Column('recipient', sa.String(length=255), nullable=False),
            sa.Column('status', sa.Enum('SENT', 'FAILED', name='notificationstatus'), nullable=False),
            sa.Column('error_message', sa.String(length=500), nullable=True),
            sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['alert_id'], ['alerts.id'], ),
            sa.ForeignKeyConstraint(['job_id'], ['notification_dispatch_jobs.id'], ),
            sa.PrimaryKeyConstraint('id')
            )

    if "notification_read_states" not in inspector.get_table_names():
        op.create_table('notification_read_states',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.UUID(), nullable=False),
            sa.Column('notification_id', sa.Integer(), nullable=False),
            sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('binned_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['notification_id'], ['notifications.id'], ),
            sa.ForeignKeyConstraint(['user_id'], ['user_profiles.id'], ),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'notification_id', name='ux_notification_read_states_user_notification')
            )


    # --- Indexes (created only if missing on their table - safe no-op
    # on any database that already has them) ---

    if "user_profiles" not in inspector.get_table_names() or "ix_user_profiles_created_at" not in _existing_index_names(inspector, "user_profiles"):
        op.create_index('ix_user_profiles_created_at', 'user_profiles', ['created_at'], unique=False)

    if "alerts" not in inspector.get_table_names() or "ix_alerts_created_at" not in _existing_index_names(inspector, "alerts"):
        op.create_index('ix_alerts_created_at', 'alerts', ['created_at'], unique=False)

    if "alerts" not in inspector.get_table_names() or "ix_alerts_is_resolved_created_at" not in _existing_index_names(inspector, "alerts"):
        op.create_index('ix_alerts_is_resolved_created_at', 'alerts', ['is_resolved', 'created_at'], unique=False)

    if "alerts" not in inspector.get_table_names() or "ix_alerts_station_id_created_at" not in _existing_index_names(inspector, "alerts"):
        op.create_index('ix_alerts_station_id_created_at', 'alerts', ['station_id', 'created_at'], unique=False)

    if "crowd_logs" not in inspector.get_table_names() or "ix_crowd_logs_station_id_created_at" not in _existing_index_names(inspector, "crowd_logs"):
        op.create_index('ix_crowd_logs_station_id_created_at', 'crowd_logs', ['station_id', 'created_at'], unique=False)

    if "crowd_logs_hourly" not in inspector.get_table_names() or "ix_crowd_logs_hourly_station_id_hour_bucket" not in _existing_index_names(inspector, "crowd_logs_hourly"):
        op.create_index('ix_crowd_logs_hourly_station_id_hour_bucket', 'crowd_logs_hourly', ['station_id', 'hour_bucket'], unique=False)

    if "enquiries" not in inspector.get_table_names() or "ix_enquiries_created_at" not in _existing_index_names(inspector, "enquiries"):
        op.create_index('ix_enquiries_created_at', 'enquiries', ['created_at'], unique=False)

    if "enquiries" not in inspector.get_table_names() or "ix_enquiries_status" not in _existing_index_names(inspector, "enquiries"):
        op.create_index('ix_enquiries_status', 'enquiries', ['status'], unique=False)

    if "enquiries" not in inspector.get_table_names() or "ix_enquiries_user_id_created_at" not in _existing_index_names(inspector, "enquiries"):
        op.create_index('ix_enquiries_user_id_created_at', 'enquiries', ['user_id', 'created_at'], unique=False)

    if "journeys" not in inspector.get_table_names() or "ix_journeys_destination_station_id_status" not in _existing_index_names(inspector, "journeys"):
        op.create_index('ix_journeys_destination_station_id_status', 'journeys', ['destination_station_id', 'status'], unique=False)

    if "journeys" not in inspector.get_table_names() or "ix_journeys_source_station_id_status" not in _existing_index_names(inspector, "journeys"):
        op.create_index('ix_journeys_source_station_id_status', 'journeys', ['source_station_id', 'status'], unique=False)

    if "journeys" not in inspector.get_table_names() or "ix_journeys_status_checkin_time" not in _existing_index_names(inspector, "journeys"):
        op.create_index('ix_journeys_status_checkin_time', 'journeys', ['status', 'checkin_time'], unique=False)

    if "journeys" not in inspector.get_table_names() or "ix_journeys_user_id_status" not in _existing_index_names(inspector, "journeys"):
        op.create_index('ix_journeys_user_id_status', 'journeys', ['user_id', 'status'], unique=False)

    if "journeys" not in inspector.get_table_names() or "ux_journeys_one_active_per_user" not in _existing_index_names(inspector, "journeys"):
        op.create_index('ux_journeys_one_active_per_user', 'journeys', ['user_id'], unique=True, postgresql_where=sa.text("status = 'ACTIVE'"))

    if "predictions" not in inspector.get_table_names() or "ix_predictions_created_at" not in _existing_index_names(inspector, "predictions"):
        op.create_index('ix_predictions_created_at', 'predictions', ['created_at'], unique=False)

    if "predictions" not in inspector.get_table_names() or "ix_predictions_station_id_type_target" not in _existing_index_names(inspector, "predictions"):
        op.create_index('ix_predictions_station_id_type_target', 'predictions', ['station_id', 'prediction_type', 'target_datetime'], unique=False)

    if "train_schedule_history" not in inspector.get_table_names() or "ix_tsh_station_id_service_date" not in _existing_index_names(inspector, "train_schedule_history"):
        op.create_index('ix_tsh_station_id_service_date', 'train_schedule_history', ['station_id', 'service_date'], unique=False)

    if "train_schedule_history" not in inspector.get_table_names() or "ix_tsh_train_id_service_date" not in _existing_index_names(inspector, "train_schedule_history"):
        op.create_index('ix_tsh_train_id_service_date', 'train_schedule_history', ['train_id', 'service_date'], unique=False)

    if "train_schedule_history" not in inspector.get_table_names() or "ix_tsh_trip_id" not in _existing_index_names(inspector, "train_schedule_history"):
        op.create_index('ix_tsh_trip_id', 'train_schedule_history', ['trip_id'], unique=False)

    if "train_schedules" not in inspector.get_table_names() or "ix_train_schedules_day_type_departure_time" not in _existing_index_names(inspector, "train_schedules"):
        op.create_index('ix_train_schedules_day_type_departure_time', 'train_schedules', ['day_type', 'departure_time'], unique=False)

    if "train_schedules" not in inspector.get_table_names() or "ix_train_schedules_delay_minutes" not in _existing_index_names(inspector, "train_schedules"):
        op.create_index('ix_train_schedules_delay_minutes', 'train_schedules', ['delay_minutes'], unique=False)

    if "train_schedules" not in inspector.get_table_names() or "ix_train_schedules_is_peak_hour" not in _existing_index_names(inspector, "train_schedules"):
        op.create_index('ix_train_schedules_is_peak_hour', 'train_schedules', ['is_peak_hour'], unique=False)

    if "train_schedules" not in inspector.get_table_names() or "ix_train_schedules_station_id_day_type" not in _existing_index_names(inspector, "train_schedules"):
        op.create_index('ix_train_schedules_station_id_day_type', 'train_schedules', ['station_id', 'day_type'], unique=False)

    if "train_schedules" not in inspector.get_table_names() or "ix_train_schedules_station_id_status" not in _existing_index_names(inspector, "train_schedules"):
        op.create_index('ix_train_schedules_station_id_status', 'train_schedules', ['station_id', 'status'], unique=False)

    if "train_schedules" not in inspector.get_table_names() or "ix_train_schedules_status" not in _existing_index_names(inspector, "train_schedules"):
        op.create_index('ix_train_schedules_status', 'train_schedules', ['status'], unique=False)

    if "train_schedules" not in inspector.get_table_names() or "ix_train_schedules_train_id" not in _existing_index_names(inspector, "train_schedules"):
        op.create_index('ix_train_schedules_train_id', 'train_schedules', ['train_id'], unique=False)

    if "notification_dispatch_jobs" not in inspector.get_table_names() or "ix_notification_dispatch_jobs_status" not in _existing_index_names(inspector, "notification_dispatch_jobs"):
        op.create_index('ix_notification_dispatch_jobs_status', 'notification_dispatch_jobs', ['status'], unique=False)

    if "notifications" not in inspector.get_table_names() or "ix_notifications_binned_at" not in _existing_index_names(inspector, "notifications"):
        op.create_index('ix_notifications_binned_at', 'notifications', ['binned_at'], unique=False)

    if "notifications" not in inspector.get_table_names() or "ix_notifications_created_at_is_read_user_id" not in _existing_index_names(inspector, "notifications"):
        op.create_index('ix_notifications_created_at_is_read_user_id', 'notifications', ['created_at', 'is_read', 'user_id'], unique=False)

    if "notification_logs" not in inspector.get_table_names() or "ix_notification_logs_alert_id_created_at" not in _existing_index_names(inspector, "notification_logs"):
        op.create_index('ix_notification_logs_alert_id_created_at', 'notification_logs', ['alert_id', 'created_at'], unique=False)

    if "notification_logs" not in inspector.get_table_names() or "ix_notification_logs_job_channel_recipient" not in _existing_index_names(inspector, "notification_logs"):
        op.create_index('ix_notification_logs_job_channel_recipient', 'notification_logs', ['job_id', 'channel', 'recipient'], unique=False)

    if "notification_read_states" not in inspector.get_table_names() or "ix_notification_read_states_notification_id" not in _existing_index_names(inspector, "notification_read_states"):
        op.create_index('ix_notification_read_states_notification_id', 'notification_read_states', ['notification_id'], unique=False)


def downgrade() -> None:
    # No-op by design - see module docstring. This is the schema
    # baseline; there is nothing before it to revert to, and dropping
    # every table here would be exactly the kind of destructive
    # operation this migration process exists to prevent.
    pass
