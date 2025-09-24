"""Dummy migration"""

from alembic import op
from sqlalchemy import text

revision = 'e08810696d4c'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """
    Dummy upgrade.
    """
    pass

def downgrade():
    """
    Dummy downgrade.
    """
    pass