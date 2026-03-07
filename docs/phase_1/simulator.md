| Class | Realism Requirement |
|---|---|
| `TransactionSimulator` | Coordinator / tick loop |
| `ArrivalProcess` | Poisson + burst traffic |
| `TransactionEngine` | State machine, retry amplification |
| `PolicyEngine` | θ-dependent dynamics |
| `GatewayModel` | Markov regime switching |
| `FailureModel` | Clustered failures per regime |
| `LatencyModel` | LogNormal heavy tail + load coupling |
| `EventStream` | JSONL append-only log |