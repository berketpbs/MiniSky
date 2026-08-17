"""
State management for tracking VM instances.

Uses SQLite to persist VM information locally so users can
manage instances across sessions.
"""

import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Optional, Any
from contextlib import contextmanager


class StateManager:
    """
    Manages persistent state of VM instances using SQLite.
    
    Storage location: ~/.minisky/state.db
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize state manager.
        
        Args:
            db_path: Custom database path (default: ~/.minisky/state.db)
        """
        if db_path is None:
            minisky_dir = Path.home() / '.minisky'
            minisky_dir.mkdir(exist_ok=True)
            db_path = str(minisky_dir / 'state.db')
        
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        """Create database schema if it doesn't exist."""
        with self._get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS vms (
                    vm_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    ssh_port INTEGER DEFAULT 22,
                    ssh_user TEXT DEFAULT 'root',
                    ssh_key_path TEXT,
                    status TEXT NOT NULL,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Used by the API server's ClusterController/JobController
            # (minisky/api/core.py) to survive restarts. Stored as a single
            # JSON blob per row rather than individual columns since
            # ClusterRecord/JobRecord are rich, API-layer-specific domain
            # objects (not the CLI's VM schema above) that don't need to be
            # queried by anything other than id/list-all.
            conn.execute('''
                CREATE TABLE IF NOT EXISTS clusters (
                    cluster_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Used by ManagedJobController (minisky/managed_jobs.py) so
            # managed job state (status, vm_id, attempts, task definition)
            # survives across separate CLI invocations and the detached
            # managed_job_runner process. Distinct from the `jobs` table
            # above, which is the API server's JobController's own record
            # type with a different id namespace and schema.
            conn.execute('''
                CREATE TABLE IF NOT EXISTS managed_jobs (
                    job_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
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
    
    def add_vm(self, vm_info: Dict[str, Any]) -> None:
        """
        Add a new VM to state tracking.
        
        Args:
            vm_info: VM information dictionary
        """
        with self._get_connection() as conn:
            # Extract metadata (anything not in core fields)
            core_fields = {
                'vm_id', 'provider', 'task_name', 'ip_address',
                'ssh_port', 'ssh_user', 'ssh_key_path', 'status'
            }
            metadata = {k: v for k, v in vm_info.items() if k not in core_fields}
            
            conn.execute('''
                INSERT INTO vms (
                    vm_id, provider, task_name, ip_address,
                    ssh_port, ssh_user, ssh_key_path, status, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                vm_info['vm_id'],
                vm_info.get('provider', 'unknown'),
                vm_info.get('task_name', 'unnamed'),
                vm_info['ip_address'],
                vm_info.get('ssh_port', 22),
                vm_info.get('ssh_user', 'root'),
                vm_info.get('ssh_key_path'),
                vm_info.get('status', 'unknown'),
                json.dumps(metadata)
            ))
            conn.commit()
    
    def get_vm(self, vm_id: str) -> Optional[Dict[str, Any]]:
        """
        Get VM information by ID.
        
        Args:
            vm_id: VM identifier
            
        Returns:
            VM info dictionary or None if not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                'SELECT * FROM vms WHERE vm_id = ?',
                (vm_id,)
            )
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            vm_info = dict(row)
            # Parse metadata JSON
            if vm_info['metadata']:
                metadata = json.loads(vm_info['metadata'])
                vm_info.update(metadata)
            del vm_info['metadata']
            
            return vm_info
    
    def list_vms(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all VMs, optionally filtered by status.
        
        Args:
            status: Filter by status (e.g., 'running', 'terminated')
            
        Returns:
            List of VM info dictionaries
        """
        with self._get_connection() as conn:
            if status:
                cursor = conn.execute(
                    'SELECT * FROM vms WHERE status = ? ORDER BY created_at DESC',
                    (status,)
                )
            else:
                cursor = conn.execute(
                    'SELECT * FROM vms ORDER BY created_at DESC'
                )
            
            vms = []
            for row in cursor.fetchall():
                vm_info = dict(row)
                if vm_info['metadata']:
                    metadata = json.loads(vm_info['metadata'])
                    vm_info.update(metadata)
                del vm_info['metadata']
                vms.append(vm_info)
            
            return vms
    
    def update_status(self, vm_id: str, status: str) -> bool:
        """
        Update VM status.
        
        Args:
            vm_id: VM identifier
            status: New status
            
        Returns:
            True if updated, False if VM not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute('''
                UPDATE vms
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE vm_id = ?
            ''', (status, vm_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def remove_vm(self, vm_id: str) -> bool:
        """
        Remove VM from tracking.
        
        Args:
            vm_id: VM identifier
            
        Returns:
            True if removed, False if not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                'DELETE FROM vms WHERE vm_id = ?',
                (vm_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def cleanup_terminated(self, older_than_days: int = 7) -> int:
        """
        Remove terminated VMs older than specified days.

        Args:
            older_than_days: Remove VMs terminated more than this many days ago

        Returns:
            Number of VMs removed
        """
        with self._get_connection() as conn:
            cursor = conn.execute('''
                DELETE FROM vms
                WHERE status = 'terminated'
                AND updated_at < datetime('now', '-' || ? || ' days')
            ''', (older_than_days,))
            conn.commit()
            return cursor.rowcount

    # -------------------------------------------------------------------
    # API server persistence: clusters
    # -------------------------------------------------------------------

    def save_cluster(self, cluster_id: str, data: Dict[str, Any]) -> None:
        """Upsert a cluster record (API server's ClusterRecord, as a dict)."""
        with self._get_connection() as conn:
            conn.execute('''
                INSERT INTO clusters (cluster_id, data, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(cluster_id) DO UPDATE SET
                    data = excluded.data,
                    updated_at = CURRENT_TIMESTAMP
            ''', (cluster_id, json.dumps(data)))
            conn.commit()

    def get_cluster_data(self, cluster_id: str) -> Optional[Dict[str, Any]]:
        """Get a persisted cluster record by ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                'SELECT data FROM clusters WHERE cluster_id = ?', (cluster_id,)
            ).fetchone()
            return json.loads(row['data']) if row else None

    def list_cluster_data(self) -> List[Dict[str, Any]]:
        """List all persisted cluster records."""
        with self._get_connection() as conn:
            rows = conn.execute('SELECT data FROM clusters').fetchall()
            return [json.loads(row['data']) for row in rows]

    def delete_cluster(self, cluster_id: str) -> bool:
        """Remove a persisted cluster record."""
        with self._get_connection() as conn:
            cursor = conn.execute('DELETE FROM clusters WHERE cluster_id = ?', (cluster_id,))
            conn.commit()
            return cursor.rowcount > 0

    # -------------------------------------------------------------------
    # API server persistence: jobs
    # -------------------------------------------------------------------

    def save_job(self, job_id: str, data: Dict[str, Any]) -> None:
        """Upsert a job record (API server's JobRecord, as a dict)."""
        with self._get_connection() as conn:
            conn.execute('''
                INSERT INTO jobs (job_id, data, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(job_id) DO UPDATE SET
                    data = excluded.data,
                    updated_at = CURRENT_TIMESTAMP
            ''', (job_id, json.dumps(data)))
            conn.commit()

    def get_job_data(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get a persisted job record by ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                'SELECT data FROM jobs WHERE job_id = ?', (job_id,)
            ).fetchone()
            return json.loads(row['data']) if row else None

    def list_job_data(self) -> List[Dict[str, Any]]:
        """List all persisted job records."""
        with self._get_connection() as conn:
            rows = conn.execute('SELECT data FROM jobs').fetchall()
            return [json.loads(row['data']) for row in rows]

    def delete_job(self, job_id: str) -> bool:
        """Remove a persisted job record."""
        with self._get_connection() as conn:
            cursor = conn.execute('DELETE FROM jobs WHERE job_id = ?', (job_id,))
            conn.commit()
            return cursor.rowcount > 0

    # -------------------------------------------------------------------
    # Managed jobs persistence (spot recovery)
    # -------------------------------------------------------------------

    def save_managed_job(self, job_id: str, data: Dict[str, Any]) -> None:
        """Upsert a managed job record (ManagedJob, serialized as a dict)."""
        with self._get_connection() as conn:
            conn.execute('''
                INSERT INTO managed_jobs (job_id, data, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(job_id) DO UPDATE SET
                    data = excluded.data,
                    updated_at = CURRENT_TIMESTAMP
            ''', (job_id, json.dumps(data)))
            conn.commit()

    def get_managed_job_data(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get a persisted managed job record by ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                'SELECT data FROM managed_jobs WHERE job_id = ?', (job_id,)
            ).fetchone()
            return json.loads(row['data']) if row else None

    def list_managed_job_data(self) -> List[Dict[str, Any]]:
        """List all persisted managed job records."""
        with self._get_connection() as conn:
            rows = conn.execute('SELECT data FROM managed_jobs').fetchall()
            return [json.loads(row['data']) for row in rows]

    def delete_managed_job(self, job_id: str) -> bool:
        """Remove a persisted managed job record."""
        with self._get_connection() as conn:
            cursor = conn.execute('DELETE FROM managed_jobs WHERE job_id = ?', (job_id,))
            conn.commit()
            return cursor.rowcount > 0
