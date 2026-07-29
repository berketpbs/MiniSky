# MiniSky Workflow Diagrams

## System Architecture

```mermaid
graph TB
    User[User] -->|minisky launch task.yaml| CLI[CLI Module]
    CLI -->|Parse YAML| Task[Task Parser]
    Task -->|Validate| TaskObj[Task Object]
    CLI -->|Get Provider| ProviderFactory[Provider Factory]
    ProviderFactory -->|Create| Provider[Cloud Provider]
    Provider -->|Launch VM| Cloud[Cloud API]
    Cloud -->|Return VM Info| Provider
    Provider -->|VM Details| State[State Manager]
    State -->|Save to| DB[(SQLite DB)]
    CLI -->|Execute Task| Executor[SSH Executor]
    Executor -->|SSH Connect| VM[Remote VM]
    Executor -->|Sync Files| VM
    Executor -->|Run Commands| VM
    VM -->|Stream Output| Executor
    Executor -->|Display| User
```

## Launch Command Flow

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant TaskParser
    participant Provider
    participant State
    participant Executor
    participant VM

    User->>CLI: minisky launch task.yaml
    CLI->>TaskParser: Parse YAML
    TaskParser->>TaskParser: Validate schema
    TaskParser-->>CLI: Task object
    CLI->>Provider: launch(task)
    Provider->>VM: Create instance
    VM-->>Provider: VM info (ID, IP)
    Provider-->>CLI: VM details
    CLI->>State: save_vm(vm_info)
    State->>State: Store in SQLite
    CLI->>Executor: execute_task(vm_info, task)
    Executor->>VM: SSH connect
    Executor->>VM: Sync workdir
    Executor->>VM: Run setup commands
    Executor->>VM: Run main commands
    VM-->>Executor: Stream output
    Executor-->>User: Display logs
    Executor-->>CLI: Task complete
    CLI-->>User: Success message
```

## Status Command Flow

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant State
    participant Provider

    User->>CLI: minisky status
    CLI->>State: list_vms()
    State->>State: Query SQLite
    State-->>CLI: List of VMs
    CLI->>Provider: status(vm_id) for each
    Provider-->>CLI: Current status
    CLI->>CLI: Format table
    CLI-->>User: Display VM table
```

## Terminate Command Flow

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant State
    participant Provider
    participant VM

    User->>CLI: minisky terminate vm-123
    CLI->>State: get_vm(vm-123)
    State-->>CLI: VM info
    CLI->>User: Confirm termination?
    User-->>CLI: Yes
    CLI->>Provider: terminate(vm-123)
    Provider->>VM: Destroy instance
    VM-->>Provider: Terminated
    Provider-->>CLI: Success
    CLI->>State: remove_vm(vm-123)
    State->>State: Delete from SQLite
    CLI-->>User: VM terminated
```

## Module Dependencies

```mermaid
graph LR
    CLI[cli.py] --> Task[task.py]
    CLI --> State[state.py]
    CLI --> Executor[executor.py]
    CLI --> Providers[providers/]
    
    Executor --> Task
    
    Providers --> Base[providers/base.py]
    Mock[providers/mock.py] -.implements.-> Base
    RunPod[providers/runpod.py] -.implements.-> Base
    
    Task --> Pydantic[Pydantic]
    Task --> PyYAML[PyYAML]
    
    CLI --> Typer[Typer]
    CLI --> Rich[Rich]
    
    Executor --> Paramiko[Paramiko]
    Executor --> Rich
    
    State --> SQLite[SQLite3]
    
    Providers --> HTTPX[HTTPX]
```

## Development Phases

```mermaid
gantt
    title MiniSky Development Timeline
    dateFormat YYYY-MM-DD
    section Phase 1: Foundation
    Project Setup           :p1, 2026-01-01, 1d
    Task Parser            :p2, after p1, 2d
    Base Provider          :p3, after p2, 1d
    Mock Provider          :p4, after p3, 2d
    State Management       :p5, after p4, 2d
    Basic CLI              :p6, after p5, 2d
    Unit Tests             :p7, after p6, 2d
    
    section Phase 2: Executor
    SSH Connection         :p8, after p7, 2d
    File Sync              :p9, after p8, 2d
    Command Execution      :p10, after p9, 2d
    Output Streaming       :p11, after p10, 1d
    Executor Tests         :p12, after p11, 2d
    
    section Phase 3: Real Provider
    RunPod API Study       :p13, after p12, 1d
    RunPod Implementation  :p14, after p13, 3d
    API Key Management     :p15, after p14, 1d
    Integration Tests      :p16, after p15, 2d
    
    section Phase 4: Polish
    Error Handling         :p17, after p16, 2d
    Documentation          :p18, after p17, 2d
    Examples               :p19, after p18, 1d
    Final Testing          :p20, after p19, 2d
```

## Data Flow: Task Execution

```mermaid
flowchart TD
    Start([User runs minisky launch]) --> LoadYAML[Load task.yaml]
    LoadYAML --> ValidateTask{Valid Task?}
    ValidateTask -->|No| Error1[Show Error]
    ValidateTask -->|Yes| GetProvider[Get Provider Instance]
    GetProvider --> LaunchVM[Launch VM on Cloud]
    LaunchVM --> WaitVM{VM Ready?}
    WaitVM -->|No| WaitVM
    WaitVM -->|Yes| SaveState[Save VM to State DB]
    SaveState --> SSHConnect[SSH Connect to VM]
    SSHConnect --> SSHSuccess{Connected?}
    SSHSuccess -->|No| Retry{Retry?}
    Retry -->|Yes| SSHConnect
    Retry -->|No| Error2[Connection Failed]
    SSHSuccess -->|Yes| SyncFiles{Has Workdir?}
    SyncFiles -->|Yes| CopyFiles[Sync Files to VM]
    SyncFiles -->|No| RunSetup
    CopyFiles --> RunSetup{Has Setup?}
    RunSetup -->|Yes| ExecSetup[Execute Setup Commands]
    RunSetup -->|No| ExecRun
    ExecSetup --> SetupSuccess{Success?}
    SetupSuccess -->|No| Error3[Setup Failed]
    SetupSuccess -->|Yes| ExecRun[Execute Run Commands]
    ExecRun --> StreamOutput[Stream Output to User]
    StreamOutput --> Complete{Completed?}
    Complete -->|No| StreamOutput
    Complete -->|Yes| Cleanup[Update State]
    Cleanup --> End([Task Complete])
    
    Error1 --> End
    Error2 --> End
    Error3 --> End
```

## Provider Interface Pattern

```mermaid
classDiagram
    class BaseProvider {
        <<abstract>>
        +config: Dict
        +launch(task: Task) VMInfo
        +status(vm_id: str) VMInfo
        +terminate(vm_id: str) bool
        +list_instances() List~VMInfo~
        +validate_resources(task: Task) bool
    }
    
    class MockProvider {
        -_instances: Dict
        -_simulate_delay: bool
        +launch(task: Task) VMInfo
        +status(vm_id: str) VMInfo
        +terminate(vm_id: str) bool
        +list_instances() List~VMInfo~
    }
    
    class RunPodProvider {
        -api_key: str
        -base_url: str
        +launch(task: Task) VMInfo
        +status(vm_id: str) VMInfo
        +terminate(vm_id: str) bool
        +list_instances() List~VMInfo~
    }
    
    class LambdaProvider {
        -api_key: str
        -base_url: str
        +launch(task: Task) VMInfo
        +status(vm_id: str) VMInfo
        +terminate(vm_id: str) bool
        +list_instances() List~VMInfo~
    }
    
    BaseProvider <|-- MockProvider
    BaseProvider <|-- RunPodProvider
    BaseProvider <|-- LambdaProvider
```

## State Management Schema

```mermaid
erDiagram
    VMS {
        string vm_id PK
        string provider
        string task_name
        string ip_address
        int ssh_port
        string ssh_user
        string ssh_key_path
        string status
        text metadata
        timestamp created_at
        timestamp updated_at
    }
```

## Task YAML Structure

```mermaid
graph TD
    Task[Task YAML] --> Name[name: string]
    Task --> Provider[provider: string]
    Task --> Resources[resources: object]
    Task --> Workdir[workdir: string optional]
    Task --> Setup[setup: list optional]
    Task --> Run[run: list required]
    Task --> Env[env: dict optional]
    
    Resources --> GPU[gpu: string]
    Resources --> GPUCount[gpu_count: int]
    Resources --> Memory[memory_gb: int]
    Resources --> Disk[disk_gb: int]
    
    Setup --> SetupCmd1[command 1]
    Setup --> SetupCmd2[command 2]
    
    Run --> RunCmd1[command 1]
    Run --> RunCmd2[command 2]
    
    Env --> EnvVar1[KEY1: value1]
    Env --> EnvVar2[KEY2: value2]
```

## Error Handling Flow

```mermaid
flowchart TD
    Operation[Any Operation] --> Try{Try Operation}
    Try -->|Success| Success[Return Result]
    Try -->|Error| Identify{Identify Error Type}
    
    Identify -->|Validation Error| ValidErr[Show Validation Message]
    Identify -->|API Error| APIErr{Retryable?}
    Identify -->|SSH Error| SSHErr{Retryable?}
    Identify -->|State Error| StateErr[Show State Error]
    
    APIErr -->|Yes| Backoff[Exponential Backoff]
    APIErr -->|No| ShowAPIErr[Show API Error]
    
    SSHErr -->|Yes| RetrySSH[Retry SSH Connection]
    SSHErr -->|No| ShowSSHErr[Show SSH Error]
    
    Backoff --> Retry{Max Retries?}
    Retry -->|No| Try
    Retry -->|Yes| ShowAPIErr
    
    RetrySSH --> RetryCount{Max Retries?}
    RetryCount -->|No| Try
    RetryCount -->|Yes| ShowSSHErr
    
    ValidErr --> Log[Log Error]
    ShowAPIErr --> Log
    ShowSSHErr --> Log
    StateErr --> Log
    
    Log --> Exit[Exit with Error Code]
    Success --> End([Complete])
```

## Testing Strategy

```mermaid
graph TB
    Tests[Test Suite] --> Unit[Unit Tests]
    Tests --> Integration[Integration Tests]
    Tests --> E2E[End-to-End Tests]
    
    Unit --> TestTask[test_task.py]
    Unit --> TestProviders[test_providers.py]
    Unit --> TestState[test_state.py]
    Unit --> TestExecutor[test_executor.py]
    
    TestTask --> TaskParsing[YAML Parsing]
    TestTask --> TaskValidation[Validation]
    
    TestProviders --> MockProvider[Mock Provider]
    TestProviders --> BaseProvider[Base Interface]
    
    TestState --> StateOps[CRUD Operations]
    TestState --> StatePersist[Persistence]
    
    TestExecutor --> SSHMock[SSH Mocking]
    TestExecutor --> FileSyncMock[File Sync Mocking]
    
    Integration --> FullWorkflow[Full Launch Workflow]
    Integration --> StateIntegration[State + Provider]
    
    E2E --> RealMock[Real Mock Provider Test]
    E2E --> CLITest[CLI Command Tests]
```

These diagrams provide a comprehensive visual overview of the MiniSky architecture, workflows, and development plan.
