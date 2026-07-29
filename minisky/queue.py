"""
Job Queue System for MiniSky.

Manages multiple jobs on VMs with status tracking,
queuing, and execution history.
"""

import sqlite3
import json
import time
from pathlib import Path
from typing import List, Dict, Optional, Any
from enum import Enum
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime


class JobStatus(str, Enum):
    """Job status enumeration."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    """Represents a job in the queue."""
    job_id: str
    vm_id: str
    command: str
    status: JobStatus
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    exit_code: Optional[int] = None
    output: Optional[str] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert job to dictionary."""
        d = asdict(self)
        d['status'] = self.status.value
        return d
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        """Create job from dictionary."""
        data['status'] = JobStatus(data['status'])
        return cls(**data)
    
    @property
    def duration(self) -> Optional[float]:
        """Get job duration in seconds."""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        elif self.started_at:
            return time.time() - self.started_at
        return None
    
    @property
    def created_at_str(self) -> str:
        """Get human-readable creation time."""
        return datetime.fromtimestamp(self.created_at).strftime("%Y-%m-%d %H:%M:%S")


class JobQueue:
    """
    Manages job queue with SQLite persistence.
    
    Features:
    - Queue multiple jobs per VM
    - Track job status (PENDING, RUNNING, COMPLETED, FAILED)
    - Store job output and errors
    - Query job history
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize job queue.
        
        Args:
            db_path: Custom database path (default: ~/.minisky/jobs.db)
        """
        if db_path is None:
            minisky_dir = Path.home() / '.minisky'
            minisky_dir.mkdir(exist_ok=True)
            db_path = str(minisky_dir / 'jobs.db')
        
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        """Create database schema if it doesn't exist."""
        with self._get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    vm_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    exit_code INTEGER,
                    output TEXT,
                    error TEXT,
                    metadata TEXT
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_jobs_vm_id ON jobs(vm_id)
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)
            ''')
            conn.commit()
    
    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    def _generate_job_id(self, vm_id: str) -> str:
        """Generate unique job ID."""
        import uuid
        short_uuid = uuid.uuid4().hex[:8]
        return f"job-{vm_id[:8]}-{short_uuid}"
    
    def add_job(self, vm_id: str, command: str, metadata: Optional[Dict] = None) -> Job:
        """
        Add a new job to the queue.
        
        Args:
            vm_id: VM identifier
            command: Command to execute
            metadata: Optional metadata
            
        Returns:
            Created Job object
        """
        job = Job(
            job_id=self._generate_job_id(vm_id),
            vm_id=vm_id,
            command=command,
            status=JobStatus.PENDING,
            created_at=time.time(),
            metadata=metadata
        )
        
        with self._get_connection() as conn:
            conn.execute('''
                INSERT INTO jobs (
                    job_id, vm_id, command, status, created_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                job.job_id,
                job.vm_id,
                job.command,
                job.status.value,
                job.created_at,
                json.dumps(metadata) if metadata else None
            ))
            conn.commit()
        
        return job
    
    def get_job(self, job_id: str) -> Optional[Job]:
        """
        Get job by ID.
        
        Args:
            job_id: Job identifier
            
        Returns:
            Job object or None if not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                'SELECT * FROM jobs WHERE job_id = ?',
                (job_id,)
            )
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            return self._row_to_job(row)
    
    def _row_to_job(self, row: sqlite3.Row) -> Job:
        """Convert database row to Job object."""
        return Job(
            job_id=row['job_id'],
            vm_id=row['vm_id'],
            command=row['command'],
            status=JobStatus(row['status']),
            created_at=row['created_at'],
            started_at=row['started_at'],
            completed_at=row['completed_at'],
            exit_code=row['exit_code'],
            output=row['output'],
            error=row['error'],
            metadata=json.loads(row['metadata']) if row['metadata'] else None
        )
    
    def list_jobs(
        self,
        vm_id: Optional[str] = None,
        status: Optional[JobStatus] = None,
        limit: int = 50
    ) -> List[Job]:
        """
        List jobs with optional filters.
        
        Args:
            vm_id: Filter by VM ID
            status: Filter by status
            limit: Maximum number of jobs to return
            
        Returns:
            List of Job objects
        """
        with self._get_connection() as conn:
            query = 'SELECT * FROM jobs WHERE 1=1'
            params = []
            
            if vm_id:
                query += ' AND vm_id = ?'
                params.append(vm_id)
            
            if status:
                query += ' AND status = ?'
                params.append(status.value)
            
            query += ' ORDER BY created_at DESC LIMIT ?'
            params.append(limit)
            
            cursor = conn.execute(query, params)
            return [self._row_to_job(row) for row in cursor.fetchall()]
    
    def get_pending_jobs(self, vm_id: str) -> List[Job]:
        """Get all pending jobs for a VM."""
        return self.list_jobs(vm_id=vm_id, status=JobStatus.PENDING)
    
    def get_running_jobs(self, vm_id: Optional[str] = None) -> List[Job]:
        """Get all running jobs."""
        return self.list_jobs(vm_id=vm_id, status=JobStatus.RUNNING)
    
    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        exit_code: Optional[int] = None,
        output: Optional[str] = None,
        error: Optional[str] = None
    ) -> bool:
        """
        Update job status.
        
        Args:
            job_id: Job identifier
            status: New status
            exit_code: Exit code (for completed/failed)
            output: Command output
            error: Error message
            
        Returns:
            True if updated, False if not found
        """
        with self._get_connection() as conn:
            now = time.time()
            
            # Set started_at when transitioning to RUNNING
            if status == JobStatus.RUNNING:
                conn.execute('''
                    UPDATE jobs
                    SET status = ?, started_at = ?
                    WHERE job_id = ? AND started_at IS NULL
                ''', (status.value, now, job_id))
            
            # Set completed_at when transitioning to terminal state
            elif status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                conn.execute('''
                    UPDATE jobs
                    SET status = ?, completed_at = ?, exit_code = ?, output = ?, error = ?
                    WHERE job_id = ?
                ''', (status.value, now, exit_code, output, error, job_id))
            
            else:
                conn.execute('''
                    UPDATE jobs SET status = ? WHERE job_id = ?
                ''', (status.value, job_id))
            
            conn.commit()
            return conn.total_changes > 0
    
    def mark_running(self, job_id: str) -> bool:
        """Mark job as running."""
        return self.update_status(job_id, JobStatus.RUNNING)
    
    def mark_completed(self, job_id: str, exit_code: int = 0, output: str = "") -> bool:
        """Mark job as completed."""
        return self.update_status(job_id, JobStatus.COMPLETED, exit_code=exit_code, output=output)
    
    def mark_failed(self, job_id: str, exit_code: int = 1, error: str = "") -> bool:
        """Mark job as failed."""
        return self.update_status(job_id, JobStatus.FAILED, exit_code=exit_code, error=error)
    
    def cancel_job(self, job_id: str) -> bool:
        """Cancel a pending job."""
        job = self.get_job(job_id)
        if job and job.status == JobStatus.PENDING:
            return self.update_status(job_id, JobStatus.CANCELLED)
        return False
    
    def remove_job(self, job_id: str) -> bool:
        """
        Remove job from queue.
        
        Args:
            job_id: Job identifier
            
        Returns:
            True if removed, False if not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                'DELETE FROM jobs WHERE job_id = ?',
                (job_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def clear_vm_jobs(self, vm_id: str, status: Optional[JobStatus] = None) -> int:
        """
        Clear all jobs for a VM.
        
        Args:
            vm_id: VM identifier
            status: Only clear jobs with this status (optional)
            
        Returns:
            Number of jobs removed
        """
        with self._get_connection() as conn:
            if status:
                cursor = conn.execute(
                    'DELETE FROM jobs WHERE vm_id = ? AND status = ?',
                    (vm_id, status.value)
                )
            else:
                cursor = conn.execute(
                    'DELETE FROM jobs WHERE vm_id = ?',
                    (vm_id,)
                )
            conn.commit()
            return cursor.rowcount
    
    def get_stats(self, vm_id: Optional[str] = None) -> Dict[str, int]:
        """
        Get job statistics.
        
        Args:
            vm_id: Filter by VM ID (optional)
            
        Returns:
            Dictionary with counts per status
        """
        with self._get_connection() as conn:
            if vm_id:
                cursor = conn.execute('''
                    SELECT status, COUNT(*) as count
                    FROM jobs WHERE vm_id = ?
                    GROUP BY status
                ''', (vm_id,))
            else:
                cursor = conn.execute('''
                    SELECT status, COUNT(*) as count
                    FROM jobs
                    GROUP BY status
                ''')
            
            stats = {s.value: 0 for s in JobStatus}
            for row in cursor.fetchall():
                stats[row['status']] = row['count']
            
            stats['total'] = sum(stats.values())
            return stats
