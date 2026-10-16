# Replay a Fantom transaction locally

Use this optional tool when historical execution traces are unavailable.
An execution trace records operations performed during a transaction.

The tool replays a transaction in a local Anvil process.
It uses saved prestate: the account and contract state from before the transaction.
It does not connect to a public RPC, write a database, or change fee acceptance rules.

## Prepare the input

1. Install Anvil.
2. Put Anvil on `PATH`, or set `ANVIL_BINARY` to its absolute path.
3. Prepare a JSON or gzip-compressed JSON file with these fields:

| Field | Contents |
| --- | --- |
| `prestate` | Observed state before the transaction. |
| `tx` | Original transaction. |
| `block` | Original block details. |
| `receipt` | Original transaction result and logs. |
| `vaults` | List of vault addresses to inspect. |
| `fork` | EVM rules: `istanbul`, `berlin`, or `london`. |

Do not invent missing state or replace the sender with a funded account.

The optional `sender_prestate_proof` field supports a limited sender-state correction.
It must contain the existing proof tied to the parent block hash.
The correction applies only when the required untouched-sender checks pass.

The tool also accepts the maintained test input files.
For these files, `event.vault_address` supplies the vault address.
`trace.local_replay.fork` supplies the EVM rules.
The tool does not use the expected trace operations to perform the replay.

## Run the replay

From the repository directory, run:

```bash
python scripts/replay_fantom_prestate.py \
  --input replay-input.json.gz --out verified-replay.json
```

The command reads saved input only. It has no option to fetch missing input.
It replaces the output file only after verification succeeds.
It does not overwrite the original evidence.

## Understand the checks

The tool checks:

- The transaction, receipt, and block identities.
- Successful execution and matching block context.
- Every receipt log.
- Gas usage under Fantom Opera rules.
- Required storage values.
- Supported call and constructor frames.

Unsupported operations, incomplete state, uncertain refunds, or mismatched logs cause an error.
A successful replay provides evidence for fee verification.
It does not authorize an estimated fee or bypass the acceptance rules.

## Read the output

The output contains compact traces grouped by vault.
It also includes the original transaction and block identities and the input file's SHA-256 hash.
A hash identifies the exact data used for the replay.

Each trace records:

- The local binary hash, prestate hash, and EVM rules.
- Whether receipt logs match.
- Whether gas usage matches under Opera rules.
- Refund data and any verified sender correction.

## Fetch input from Python

Python callers can use `fetch_replay_traces` to fetch missing native input.
This helper sends one batch with fixed limits and checks each response identity.
It limits the response to 20 MiB.

The offline command does not call this helper.
The helper does not require archived migration tools.

## Run the local execution tests

The normal test suite runs the offline checks.
To replay all seven saved cases with Anvil, run:

```bash
RUN_LOCAL_EVM_REPLAY=1 pytest tests/test_fantom_prestate_replay.py
```
