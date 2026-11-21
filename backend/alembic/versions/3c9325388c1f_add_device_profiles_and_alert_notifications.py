"""add device alert profiles and notifications

Revision ID: 3c9325388c1f
Revises: 2b8214277b0e
Create Date: 2026-11-21 14:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import geoalchemy2.types

# revision identifiers, used by Alembic.
revision: str = '3c9325388c1f'
down_revision: Union[str, None] = '2b8214277b0e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create DeviceAlertProfile
    op.create_table(
        'device_alert_profiles',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('device_id', sa.String(length=128), nullable=False),
        sa.Column('push_token', sa.String(length=512), nullable=True),
        sa.Column('platform', sa.String(length=32), server_default='unknown', nullable=False),
        sa.Column('alert_earthquake', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('alert_flood', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('alert_cyclone', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('alert_critical', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('alert_advisory', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('notification_radius_km', sa.Numeric(precision=6, scale=2), server_default='50.0', nullable=False),
        sa.Column('sound_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('last_known_location', geoalchemy2.types.Geometry(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeomFromEWKT', name='geometry'), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_active_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('length(trim(device_id)) > 0', name='device_id_nonempty'),
        sa.CheckConstraint('notification_radius_km > 0', name='radius_positive'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_id'),
    )
    op.create_index('ix_device_alert_profiles_active', 'device_alert_profiles', ['is_active'], unique=False)
    op.create_index('ix_device_alert_profiles_device_id', 'device_alert_profiles', ['device_id'], unique=True)

    # 2. Create AlertNotificationRecord
    crisis_type_enum = postgresql.ENUM('EARTHQUAKE', 'FLOOD', 'CYCLONE', name='crisis_type', create_type=False)
    alert_classification_enum = sa.Enum(
        'OFFICIAL_WARNING',
        'OFFICIAL_ADVISORY',
        'NEW_DISASTER_EVENT',
        'WARNING_ESCALATION',
        'WARNING_UPDATE',
        'WARNING_CANCELLED',
        'WARNING_EXPIRED',
        'SIGNIFICANT_HAZARD_CHANGE',
        'DATA_SOURCE_FAILURE',
        name='alert_classification',
    )
    alert_source_kind_enum = sa.Enum(
        'OFFICIAL_GOVERNMENT_WARNING',
        'OFFICIAL_OBSERVATION',
        'SYSTEM_GENERATED_ASSESSMENT',
        'TECHNICAL_NOTIFICATION',
        name='alert_source_kind',
    )

    op.create_table(
        'alert_notifications',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('device_id', sa.String(length=128), nullable=False),
        sa.Column('crisis_event_id', sa.Uuid(), nullable=True),
        sa.Column('crisis_source_report_id', sa.Uuid(), nullable=True),
        sa.Column('disaster_type', crisis_type_enum, nullable=False),
        sa.Column('alert_classification', alert_classification_enum, nullable=False),
        sa.Column('alert_source_kind', alert_source_kind_enum, nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('original_severity', sa.String(length=100), nullable=True),
        sa.Column('issuing_authority', sa.String(length=255), nullable=True),
        sa.Column('source_url', sa.String(length=2048), nullable=True),
        sa.Column('deep_link_url', sa.String(length=512), nullable=True),
        sa.Column('delivery_status', sa.String(length=50), server_default='SIMULATED', nullable=False),
        sa.Column('delivery_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('is_read', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['crisis_event_id'], ['crisis_events.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['crisis_source_report_id'], ['crisis_source_reports.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_alert_notifications_created_at', 'alert_notifications', ['created_at'], unique=False)
    op.create_index('ix_alert_notifications_device_id', 'alert_notifications', ['device_id'], unique=False)
    op.create_index('ix_alert_notifications_is_read', 'alert_notifications', ['is_read'], unique=False)

    # 3. Create AlertSyncLog
    op.create_table(
        'alert_sync_logs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('source_code', sa.String(length=100), nullable=False),
        sa.Column('sync_mode', sa.String(length=50), server_default='SCHEDULED', nullable=False),
        sa.Column('status', sa.String(length=50), server_default='SUCCESS', nullable=False),
        sa.Column('events_detected', sa.Integer(), server_default='0', nullable=False),
        sa.Column('alerts_generated', sa.Integer(), server_default='0', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_alert_sync_logs_source_code', 'alert_sync_logs', ['source_code'], unique=False)
    op.create_index('ix_alert_sync_logs_started_at', 'alert_sync_logs', ['started_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_alert_sync_logs_started_at', table_name='alert_sync_logs')
    op.drop_index('ix_alert_sync_logs_source_code', table_name='alert_sync_logs')
    op.drop_table('alert_sync_logs')

    op.drop_index('ix_alert_notifications_is_read', table_name='alert_notifications')
    op.drop_index('ix_alert_notifications_device_id', table_name='alert_notifications')
    op.drop_index('ix_alert_notifications_created_at', table_name='alert_notifications')
    op.drop_table('alert_notifications')

    op.drop_index('ix_device_alert_profiles_device_id', table_name='device_alert_profiles')
    op.drop_index('ix_device_alert_profiles_active', table_name='device_alert_profiles')
    op.drop_table('device_alert_profiles')

    sa.Enum(name='alert_source_kind').drop(op.get_bind(), checkfirst=False)
    sa.Enum(name='alert_classification').drop(op.get_bind(), checkfirst=False)
