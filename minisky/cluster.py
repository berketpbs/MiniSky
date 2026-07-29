"""
Multi-Node Cluster Management for MiniSky.

Provides support for launching and managing multi-node clusters
for distributed training with PyTorch, DeepSpeed, etc.
"""

import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from rich.console import Console
from rich.table import Table

console = Console()


class NodeRole(str, Enum):
    """Node role in the cluster."""
    HEAD = "head"
    WORKER = "worker"


@dataclass
class ClusterNode:
    """Represents a node in the cluster."""
    vm_id: str
    role: NodeRole
    ip_address: str
    private_ip: Optional[str] = None
    rank: int = 0
    status: str = "running"
    
    @property
    def is_head(self) -> bool:
        return self.role == NodeRole.HEAD


@dataclass
class Cluster:
    """
    Represents a multi-node cluster.
    
    A cluster consists of:
    - 1 head node (rank 0)
    - N-1 worker nodes (rank 1 to N-1)
    """
    cluster_id: str
    task_name: str
    num_nodes: int
    nodes: List[ClusterNode] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    
    @property
    def head_node(self) -> Optional[ClusterNode]:
        """Get the head node."""
        for node in self.nodes:
            if node.is_head:
                return node
        return None
    
    @property
    def worker_nodes(self) -> List[ClusterNode]:
        """Get all worker nodes."""
        return [n for n in self.nodes if not n.is_head]
    
    @property
    def is_ready(self) -> bool:
        """Check if all nodes are running."""
        return (
            len(self.nodes) == self.num_nodes and
            all(n.status == "running" for n in self.nodes)
        )
    
    @property
    def master_addr(self) -> Optional[str]:
        """Get master address (head node IP)."""
        head = self.head_node
        return head.private_ip or head.ip_address if head else None
    
    def get_distributed_env(self, node: ClusterNode, master_port: int = 29500) -> Dict[str, str]:
        """
        Get environment variables for distributed training.
        
        Args:
            node: The node to get env vars for
            master_port: Port for distributed communication
            
        Returns:
            Dictionary of environment variables
        """
        return {
            "MASTER_ADDR": self.master_addr or "localhost",
            "MASTER_PORT": str(master_port),
            "WORLD_SIZE": str(self.num_nodes),
            "RANK": str(node.rank),
            "LOCAL_RANK": "0",  # Assuming single GPU per node for simplicity
            "NODE_RANK": str(node.rank),
            # PyTorch specific
            "NCCL_DEBUG": "INFO",
            "NCCL_SOCKET_IFNAME": "eth0",
            # DeepSpeed specific
            "DEEPSPEED_HOSTFILE": "/tmp/hostfile",
        }
    
    def get_torchrun_cmd(
        self,
        node: ClusterNode,
        script: str,
        nproc_per_node: int = 1,
        master_port: int = 29500
    ) -> str:
        """
        Generate torchrun command for a node.
        
        Args:
            node: The node to run on
            script: Python script to run
            nproc_per_node: Number of processes per node
            master_port: Port for distributed communication
            
        Returns:
            torchrun command string
        """
        return (
            f"torchrun "
            f"--nnodes={self.num_nodes} "
            f"--nproc_per_node={nproc_per_node} "
            f"--node_rank={node.rank} "
            f"--master_addr={self.master_addr} "
            f"--master_port={master_port} "
            f"{script}"
        )
    
    def generate_hostfile(self) -> str:
        """
        Generate hostfile for DeepSpeed/MPI.
        
        Returns:
            Hostfile content
        """
        lines = []
        for node in sorted(self.nodes, key=lambda n: n.rank):
            ip = node.private_ip or node.ip_address
            lines.append(f"{ip} slots=1")  # Assuming 1 GPU per node
        return "\n".join(lines)


class ClusterManager:
    """
    Manages multi-node clusters.
    
    Usage:
        manager = ClusterManager(state, provider)
        cluster = manager.create_cluster(task, num_nodes=4)
        manager.setup_networking(cluster)
        manager.run_distributed(cluster, "python train.py")
    """
    
    def __init__(self, state_manager: Any, provider: Any):
        """
        Initialize cluster manager.
        
        Args:
            state_manager: StateManager instance
            provider: Cloud provider instance
        """
        self.state = state_manager
        self.provider = provider
        self._clusters: Dict[str, Cluster] = {}
    
    def create_cluster(self, task: Any, num_nodes: int = 2) -> Cluster:
        """
        Create a multi-node cluster.
        
        Args:
            task: Task definition
            num_nodes: Number of nodes to launch
            
        Returns:
            Cluster object
        """
        import uuid
        
        cluster_id = f"cluster-{uuid.uuid4().hex[:8]}"
        cluster = Cluster(
            cluster_id=cluster_id,
            task_name=task.name,
            num_nodes=num_nodes
        )
        
        console.print(f"[cyan]Creating cluster with {num_nodes} nodes...[/cyan]")
        
        # Launch head node first
        console.print("[cyan]Launching head node (rank 0)...[/cyan]")
        head_vm = self.provider.launch(task)
        head_node = ClusterNode(
            vm_id=head_vm['vm_id'],
            role=NodeRole.HEAD,
            ip_address=head_vm['ip_address'],
            private_ip=head_vm.get('private_ip'),
            rank=0
        )
        cluster.nodes.append(head_node)
        self.state.add_vm({**head_vm, 'cluster_id': cluster_id, 'node_role': 'head'})
        console.print(f"[green]✓[/green] Head node: {head_vm['vm_id']} ({head_vm['ip_address']})")
        
        # Launch worker nodes
        for i in range(1, num_nodes):
            console.print(f"[cyan]Launching worker node (rank {i})...[/cyan]")
            worker_vm = self.provider.launch(task)
            worker_node = ClusterNode(
                vm_id=worker_vm['vm_id'],
                role=NodeRole.WORKER,
                ip_address=worker_vm['ip_address'],
                private_ip=worker_vm.get('private_ip'),
                rank=i
            )
            cluster.nodes.append(worker_node)
            self.state.add_vm({**worker_vm, 'cluster_id': cluster_id, 'node_role': 'worker', 'rank': i})
            console.print(f"[green]✓[/green] Worker node {i}: {worker_vm['vm_id']} ({worker_vm['ip_address']})")
        
        self._clusters[cluster_id] = cluster
        
        console.print(f"\n[green]✓[/green] Cluster created: {cluster_id}")
        console.print(f"  Nodes: {num_nodes}")
        console.print(f"  Master: {cluster.master_addr}")
        
        return cluster
    
    def get_cluster(self, cluster_id: str) -> Optional[Cluster]:
        """Get cluster by ID."""
        return self._clusters.get(cluster_id)
    
    def list_clusters(self) -> List[Cluster]:
        """List all clusters."""
        return list(self._clusters.values())
    
    def setup_networking(self, cluster: Cluster, executor_factory: Any) -> bool:
        """
        Setup networking between cluster nodes.
        
        This includes:
        - SSH key distribution for passwordless SSH
        - /etc/hosts configuration
        - Hostfile generation for DeepSpeed
        
        Args:
            cluster: Cluster to setup
            executor_factory: Factory function to create executors
            
        Returns:
            True if successful
        """
        console.print("[cyan]Setting up cluster networking...[/cyan]")
        
        head = cluster.head_node
        if not head:
            console.print("[red]No head node found[/red]")
            return False
        
        # Generate hostfile
        hostfile = cluster.generate_hostfile()
        
        # Setup each node
        for node in cluster.nodes:
            vm_info = self.state.get_vm(node.vm_id)
            if not vm_info:
                continue
            
            executor = executor_factory(vm_info)
            try:
                executor.connect()
                
                # Write hostfile
                executor.execute_command(
                    f"echo '{hostfile}' > /tmp/hostfile",
                    stream_output=False
                )
                
                # Add all nodes to /etc/hosts
                hosts_entries = []
                for n in cluster.nodes:
                    ip = n.private_ip or n.ip_address
                    hosts_entries.append(f"{ip} node{n.rank}")
                
                hosts_cmd = " && ".join([
                    f"echo '{entry}' >> /etc/hosts"
                    for entry in hosts_entries
                ])
                executor.execute_command(hosts_cmd, stream_output=False)
                
                console.print(f"[green]✓[/green] Configured node{node.rank}")
                
            except Exception as e:
                console.print(f"[red]Error configuring node{node.rank}:[/red] {str(e)}")
                return False
            finally:
                executor.disconnect()
        
        console.print("[green]✓[/green] Cluster networking configured")
        return True
    
    def run_distributed(
        self,
        cluster: Cluster,
        command: str,
        executor_factory: Any,
        use_torchrun: bool = True,
        nproc_per_node: int = 1,
        master_port: int = 29500
    ) -> Dict[str, int]:
        """
        Run a distributed command on all nodes.
        
        Args:
            cluster: Cluster to run on
            command: Command to execute
            executor_factory: Factory function to create executors
            use_torchrun: Whether to wrap with torchrun
            nproc_per_node: Processes per node
            master_port: Master port for communication
            
        Returns:
            Dictionary mapping vm_id to exit code
        """
        results = {}
        
        console.print(f"[cyan]Running distributed command on {cluster.num_nodes} nodes...[/cyan]")
        
        for node in sorted(cluster.nodes, key=lambda n: n.rank):
            vm_info = self.state.get_vm(node.vm_id)
            if not vm_info:
                results[node.vm_id] = -1
                continue
            
            # Get distributed environment
            env = cluster.get_distributed_env(node, master_port)
            
            # Wrap command with torchrun if requested
            if use_torchrun and command.endswith('.py'):
                cmd = cluster.get_torchrun_cmd(node, command, nproc_per_node, master_port)
            else:
                cmd = command
            
            console.print(f"\n[cyan]Node {node.rank} ({node.vm_id}):[/cyan]")
            console.print(f"  Command: {cmd}")
            
            executor = executor_factory(vm_info)
            try:
                executor.connect()
                exit_code = executor.execute_command(cmd, env=env)
                results[node.vm_id] = exit_code
                
                if exit_code == 0:
                    console.print(f"[green]✓[/green] Node {node.rank} completed")
                else:
                    console.print(f"[red]✗[/red] Node {node.rank} failed (exit code: {exit_code})")
                    
            except Exception as e:
                console.print(f"[red]Error on node {node.rank}:[/red] {str(e)}")
                results[node.vm_id] = -1
            finally:
                executor.disconnect()
        
        return results
    
    def terminate_cluster(self, cluster: Cluster) -> bool:
        """
        Terminate all nodes in a cluster.
        
        Args:
            cluster: Cluster to terminate
            
        Returns:
            True if all nodes terminated successfully
        """
        console.print(f"[cyan]Terminating cluster {cluster.cluster_id}...[/cyan]")
        
        success = True
        for node in cluster.nodes:
            try:
                self.provider.terminate(node.vm_id)
                self.state.remove_vm(node.vm_id)
                console.print(f"[green]✓[/green] Terminated node{node.rank}: {node.vm_id}")
            except Exception as e:
                console.print(f"[red]Error terminating node{node.rank}:[/red] {str(e)}")
                success = False
        
        if cluster.cluster_id in self._clusters:
            del self._clusters[cluster.cluster_id]
        
        return success
    
    def display_cluster(self, cluster: Cluster) -> None:
        """Display cluster status in a table."""
        table = Table(title=f"Cluster: {cluster.cluster_id}")
        table.add_column("Rank", style="cyan")
        table.add_column("Role", style="magenta")
        table.add_column("VM ID", style="blue")
        table.add_column("IP Address", style="green")
        table.add_column("Status", style="yellow")
        
        for node in sorted(cluster.nodes, key=lambda n: n.rank):
            table.add_row(
                str(node.rank),
                node.role.value,
                node.vm_id,
                node.ip_address,
                node.status
            )
        
        console.print(table)
        console.print(f"\nMaster Address: {cluster.master_addr}")
        console.print(f"Ready: {'Yes' if cluster.is_ready else 'No'}")
