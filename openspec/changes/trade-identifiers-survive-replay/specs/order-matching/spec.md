## ADDED Requirements

### Requirement: Trade identifiers are unique and reproducible

Each execution SHALL carry an identifier that names it and no other execution,
across every instrument the engine holds, for that engine's lifetime. The
identifier SHALL be reported to the caller whose submission or modification
caused the execution, and SHALL be assigned whether or not a sink is attached.

Replaying a recorded session into a fresh engine SHALL assign each execution it
re-derives the identifier the recorded session assigned to the execution at the
same position in that session's sequence of executions, so that a replayed run
and the recording it was built from can be joined on the identifier.

Nothing further is promised. Identifiers are not required to be dense, to
increase along the recorded stream, to begin at any particular value, or to
bear any relation to an order identifier, a priority stamp or an event sequence
number — a recorded stream's ordering key is its sequence number, which
`recording-sink` already guarantees is monotonically increasing. The scope is
one engine, not the process: identifiers assigned by two engines are unrelated,
and nothing may be inferred from comparing them.

#### Scenario: Executions on two instruments do not share identifiers

- **WHEN** executions occur alternately on two instruments of one engine, with
  no sink attached
- **THEN** every execution reported to the caller carries an identifier, and no
  two of them carry the same one

#### Scenario: A replay re-derives the identifiers it recorded

- **WHEN** a recorded session is replayed into a fresh engine, which re-matches
  to derive its executions rather than reading the recorded ones back
- **THEN** the replay reports the same number of executions, and each carries
  the identifier the recorded session's execution in that position carried
