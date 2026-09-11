"""Database-time leases work across Vercel instances and transaction poolers."""
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, update, or_, func
from .models import JobLease
from .metrics import aware

LEASE_SECONDS = 360  # Longer than Vercel's configured 300s hard request limit.
SYNC_LEASE = "youtube-sync"


def acquire(session_factory, bucket=None, name=SYNC_LEASE):
    owner = uuid4().hex
    with session_factory.begin() as s:
        if s.bind.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        s.execute(insert(JobLease).values(name=name).on_conflict_do_nothing(index_elements=["name"]))
        now = aware(s.scalar(select(func.now())))
        conditions = [JobLease.name == name,
                      or_(JobLease.expires_at.is_(None), JobLease.expires_at <= now)]
        if bucket is not None:
            conditions.append(or_(JobLease.completed_bucket.is_(None), JobLease.completed_bucket != bucket))
        updated = s.execute(update(JobLease).where(*conditions).values(
            owner=owner, expires_at=now+timedelta(seconds=LEASE_SECONDS)))
        if updated.rowcount == 1:
            return owner, None
        row = s.get(JobLease, name)
        return None, "already_completed" if bucket and row.completed_bucket == bucket else "already_running"


def release(session_factory, owner, bucket=None, name=SYNC_LEASE):
    with session_factory.begin() as s:
        values = {"owner": None, "expires_at": None}
        if bucket is not None:
            values["completed_bucket"] = bucket
        s.execute(update(JobLease).where(JobLease.name == name, JobLease.owner == owner).values(**values))
