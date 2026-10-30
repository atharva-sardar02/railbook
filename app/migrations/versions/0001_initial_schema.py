"""initial schema

Revision ID: 0001
Revises: 
"""

import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('stations',
    sa.Column('code', sa.String(length=8), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.PrimaryKeyConstraint('code')
    )
    op.create_table('trains',
    sa.Column('number', sa.String(length=10), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('from_station', sa.String(length=8), nullable=False),
    sa.Column('to_station', sa.String(length=8), nullable=False),
    sa.Column('departs_at', sa.Time(), nullable=False),
    sa.Column('arrives_at', sa.Time(), nullable=False),
    sa.ForeignKeyConstraint(['from_station'], ['stations.code'], ),
    sa.ForeignKeyConstraint(['to_station'], ['stations.code'], ),
    sa.PrimaryKeyConstraint('number')
    )
    op.create_table('schedules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('train_number', sa.String(length=10), nullable=False),
    sa.Column('travel_date', sa.Date(), nullable=False),
    sa.ForeignKeyConstraint(['train_number'], ['trains.number'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('train_number', 'travel_date')
    )
    op.create_index(op.f('ix_schedules_travel_date'), 'schedules', ['travel_date'], unique=False)
    op.create_table('bookings',
    sa.Column('pnr', sa.String(length=10), nullable=False),
    sa.Column('schedule_id', sa.Integer(), nullable=False),
    sa.Column('class_code', sa.String(length=4), nullable=False),
    sa.Column('passenger_name', sa.String(length=100), nullable=False),
    sa.Column('seats', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
              nullable=False),
    sa.ForeignKeyConstraint(['schedule_id'], ['schedules.id'], ),
    sa.PrimaryKeyConstraint('pnr')
    )
    op.create_table('inventory',
    sa.Column('schedule_id', sa.Integer(), nullable=False),
    sa.Column('class_code', sa.String(length=4), nullable=False),
    sa.Column('total', sa.Integer(), nullable=False),
    sa.Column('available', sa.Integer(), nullable=False),
    sa.Column('fare', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.CheckConstraint('available >= 0 AND available <= total'),
    sa.ForeignKeyConstraint(['schedule_id'], ['schedules.id'], ),
    sa.PrimaryKeyConstraint('schedule_id', 'class_code')
    )


def downgrade() -> None:
    op.drop_table('inventory')
    op.drop_table('bookings')
    op.drop_index(op.f('ix_schedules_travel_date'), table_name='schedules')
    op.drop_table('schedules')
    op.drop_table('trains')
    op.drop_table('stations')
