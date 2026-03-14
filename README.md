# Synthesis Yield Agent

**Autonomous DeFi yield optimization agent** — scans lending protocols for the best USDC supply rates, allocates capital with risk-adjusted scoring, and manages positions with circuit breakers and safety rails.

Built for the [Ethereum Foundation Synthesis Hackathon](https://synthesis.md/) (March 13-22, 2026).

> Track: "Agents that pay" — What happens when agents move your money?

## What It Does

The agent autonomously:

1. **Scans** — Fetches USDC yield rates from Aave V3, Morpho Blue, and Compound V3 on Base, cross-validated across DeFi Llama + on-chain contract reads
2. **Scores** — Evaluates protocol risk (TVL, age, audits, utilization, bad debt history) and computes net APY after gas costs
3. **Allocates** — Distributes capital proportionally to risk-adjusted yield, respecting per-protocol caps and reserve buffers
4. **Executes** — Deposits/withdraws in paper mode (simulated) or live mode (on-chain), with full audit trail
5. **Monitors** — Runs circuit breakers every cycle (USDC depeg, TVL crash, gas freeze, rate divergence), auto-emergency-withdraws on critical conditions

```
        ┌─────────────────────────────┐
        │       Yield Agent Core      │
        │   (autonomous loop)         │
        └──────────┬──────────────────┘
                   │
      ┌────────────┼────────────────┐
      │            │                │
┌─────▼──────┐ ┌──▼───────┐ ┌──────▼───────┐
│ Multi-Src  │ │ Strategy │ │  Execution   │
│ Data Layer │ │ Engine   │ │  + Safety    │
└─────┬──────┘ └──┬───────┘ └──────┬───────┘
      │            │                │
      ▼            ▼                ▼
  DeFi Llama   Risk scoring,    Paper/live
  + on-chain   allocation,     deposit/withdraw
  reads        rebalancing     + circuit breakers
```

## Quick Start

```bash
# Clone and setup
git clone https://github.com/SenorCodigo69/synthesis-yield-agent.git
cd synthesis-yield-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Scan live rates
python -m src scan

# See allocation plan
python -m src allocate

# Run health check
python -m src health

# Paper-mode execution
python -m src execute

# Full agent loop
python -m src run
```

## CLI Commands

| Command | Description |
|---|---|
| `scan` | Live USDC yield rates across protocols |
| `allocate` | Optimal allocation plan with risk analysis |
| `execute` | Execute allocation (paper mode by default) |
| `run` | Continuous agent loop (scan + allocate + execute) |
| `health` | System health check (circuit breakers + protocol health) |
| `dashboard` | Audit trail with P&L summary and yield curve |
| `portfolio` | Current portfolio state from database |
| `history` | Execution history log |
| `emergency-withdraw` | Instant full withdrawal (bypasses cooldowns) |

All commands support `--json-output` for piping.

## Architecture

### Multi-Source Data Layer

Every data point is cross-validated before any capital moves:

| Data | Source 1 | Source 2 |
|---|---|---|
| Supply APY | DeFi Llama yields API | On-chain contract reads |
| TVL | DeFi Llama | On-chain totalSupply |
| Utilization | DeFi Llama | On-chain getUtilization |
| Gas price | On-chain baseFeePerGas | Default fallback |
| USDC price | CoinGecko | DeFi Llama stablecoins |

**Cross-validation rules:**
- Rate divergence > 0.5% between sources: warning, use median
- Rate divergence > 2%: block all actions for that protocol
- Single source only: log warning, proceed with caution

### Strategy Engine

- **Risk scoring** — 5-factor weighted score (TVL 25%, age 20%, audits 20%, utilization 20%, bad debt 15%)
- **Net APY** — Gross APY minus amortized gas costs over expected hold period
- **Risk-adjusted yield** — `net_apy * (1 - risk_score)` — protocols are ranked by this
- **Allocation** — Proportional to risk-adjusted yield, with per-protocol caps and redistribution

### Safety Rails

**Spending scope constraints (configurable in `config/default.yaml`):**

| Constraint | Default |
|---|---|
| Max total allocation | 80% of capital |
| Max per protocol | 40% of allocation |
| Min protocol TVL | $50M |
| Max utilization | 90% |
| Max APY (sanity cap) | 50% |
| Withdrawal cooldown | 1 hour |
| Reserve buffer | 20% kept liquid |

**Circuit breakers (run every cycle):**

| Breaker | Threshold | Action |
|---|---|---|
| USDC depeg | > 0.5% from $1.00 | Emergency withdraw ALL |
| TVL crash | > 30% drop in 1h | Emergency withdraw protocol |
| Gas freeze | > 200 gwei | Freeze all moves |
| Rate divergence | > 2% between sources | Pause protocol |

**Health monitor** — 6 checks per protocol before execution:
1. Rate cross-validation passed
2. TVL above minimum
3. Utilization below cap
4. APY sanity check
5. Not frozen by circuit breaker
6. No critical breaker trips

### Protocols

| Protocol | Chain | Status |
|---|---|---|
| Aave V3 | Base | Active — $103M TVL, 2.5% APY |
| Morpho Blue (MetaMorpho) | Base | Active — $411M TVL, 3.6% APY |
| Compound V3 | Base | Monitored — $2.2M TVL (below $50M minimum) |

### Execution Modes

| Mode | Behavior |
|---|---|
| **Paper** (default) | Simulates trades, tracks portfolio in SQLite, accrues yield |
| **Dry run** | Builds real transactions but doesn't sign/send |
| **Live** | Full on-chain execution via protocol adapters |

## Security

**5 security audits completed** — all findings fixed before merge:

| Audit | Findings | Fixed |
|---|---|---|
| #1 — Data layer | 9 (2 CRIT, 3 HIGH) | All |
| #2 — Strategy engine | 7 (3 CRIT, 4 HIGH) | All |
| #3 — Execution engine | 3 HIGH + 6 MEDIUM | All |
| #4 — Circuit breakers | 2 HIGH + 3 MEDIUM | All |
| #5 — Full codebase | 1 HIGH + 2 MEDIUM | All |

Key security measures:
- Private key isolated via `TransactionSigner` — never stored in config
- Chain ID validated at startup + enforced per transaction
- Dynamic gas estimation with fallback
- Transaction receipt timeout (120s) prevents infinite hangs
- ERC-4626 slippage protection on Morpho withdrawals
- Amount sanity cap ($1B max)
- Depeg price validated against [$0.50, $1.50] bounds
- Spending scope config validated on load

## Testing

```bash
pytest tests/           # 185 tests
pytest tests/ -v        # Verbose output
pytest tests/ --tb=short -q  # Quick summary
```

**185 tests** covering data layer, protocols, strategy, execution, portfolio, circuit breakers, health monitor, and security.

## Configuration

All runtime config in `config/default.yaml`:

```yaml
spending_scope:
  max_total_allocation_pct: 0.80
  max_per_protocol_pct: 0.40
  min_protocol_tvl_usd: 50_000_000
  max_utilization: 0.90
  max_apy_sanity: 0.50

circuit_breakers:
  depeg_threshold: 0.005
  tvl_drop_1h_pct: 0.30
  gas_freeze_gwei: 200
```

Environment variables (`.env`):
- `BASE_RPC_URL` — Base chain RPC endpoint
- `PRIVATE_KEY` — Wallet private key (live mode only)
- `BLOCKNATIVE_API_KEY` — Optional gas oracle

## Tech Stack

- **Python 3.13** — agent logic
- **web3.py** — on-chain contract reads + transaction execution
- **aiohttp** — async API calls (DeFi Llama, CoinGecko)
- **SQLite** (aiosqlite) — portfolio state, execution log, audit trail
- **Click** — CLI framework
- **Circom + snarkjs** — ZK proof generation (privacy layer)

## Project Structure

```
src/
├── main.py              # CLI entry point (9 commands)
├── config.py            # YAML config + spending scope validation
├── models.py            # Data models (16 dataclasses/enums)
├── database.py          # SQLite persistence
├── executor.py          # Paper/dry-run/live execution engine
├── portfolio.py         # Position tracking + yield accrual
├── circuit_breakers.py  # Depeg, TVL crash, gas freeze, rate divergence
├── health_monitor.py    # Pre-execution health checks (6 per protocol)
├── depeg_monitor.py     # Live USDC price fetching + validation
├── data/
│   ├── aggregator.py    # Cross-validation engine
│   ├── defillama.py     # DeFi Llama API client
│   ├── gas.py           # Gas price tracking
│   └── onchain.py       # Direct contract reads (Aave, Compound)
├── protocols/
│   ├── base.py          # Abstract adapter interface
│   ├── aave_v3.py       # Aave V3 adapter
│   ├── morpho_blue.py   # Morpho Blue (MetaMorpho ERC-4626) adapter
│   ├── compound_v3.py   # Compound V3 adapter
│   ├── abis.py          # Shared ABI fragments
│   └── tx_helpers.py    # Transaction signing + safety
└── strategy/
    ├── allocator.py     # Capital allocation engine
    ├── risk_scorer.py   # 5-factor protocol risk scoring
    ├── net_apy.py       # Net APY after gas costs
    └── rebalancer.py    # Rebalance trigger engine
```

## Hackathon Details

- **Event:** Ethereum Foundation Synthesis Hackathon
- **Track:** "Agents that pay"
- **Building period:** March 13-22, 2026
- **Primary AI:** Claude Opus 4.6 via claude-code
- **Conversation log:** [`docs/hackathon/CONVERSATION-LOG.md`](https://github.com/SenorCodigo69/finance_agent/blob/main/docs/hackathon/CONVERSATION-LOG.md)

## License

MIT
