# Synthesis Yield Agent

**Autonomous DeFi yield optimization agent with AI-powered swap reasoning** — scans lending protocols for the best USDC supply rates, uses Claude AI to decide when to swap tokens, executes via Uniswap Trading API, and deposits into yield protocols. All with circuit breakers and safety rails.

Built for the [Ethereum Foundation Synthesis Hackathon](https://synthesis.md/) (March 13-22, 2026).

> Track: "Agents that pay" — What happens when agents move your money?
>
> Bounty: "Agentic Finance — Best Uniswap API Integration"

## What It Does

The agent autonomously runs a full DeFi capital management loop:

```
  ┌─────────────────────────────────────────────────────────────┐
  │                   AUTONOMOUS AGENT LOOP                     │
  │                                                             │
  │   1. SCAN       Fetch yield rates (DeFi Llama + on-chain)  │
  │        ↓                                                    │
  │   2. THINK      AI analyzes rates, balances, market data   │
  │        ↓        (Claude Haiku or rule-based fallback)       │
  │        ↓                                                    │
  │   3. SWAP       Execute via Uniswap Trading API            │
  │        ↓        (Permit2, optimal routing, V2/V3/V4)       │
  │        ↓                                                    │
  │   4. EARN       Deposit USDC into best-yield protocol      │
  │        ↓        (Aave V3, Morpho Blue on Base)             │
  │        ↓                                                    │
  │   5. MONITOR    Circuit breakers every cycle                │
  │        ↓        (depeg, TVL crash, gas, rate divergence)    │
  │        ↓                                                    │
  │   └── REPEAT ──────────────────────────────────────────┘    │
  └─────────────────────────────────────────────────────────────┘
```

1. **Scans** — Fetches USDC yield rates from Aave V3, Morpho Blue, and Compound V3 on Base, cross-validated across DeFi Llama + on-chain reads
2. **Thinks** — Claude AI analyzes yield rates, wallet balances, and gas costs, then recommends the optimal action (swap, deposit, or hold)
3. **Swaps** — Executes token swaps via the Uniswap Trading API with Permit2 flow, optimal routing across V2/V3/V4 pools and UniswapX
4. **Earns** — Deposits USDC into the highest risk-adjusted yield protocol
5. **Monitors** — Circuit breakers check for USDC depeg, TVL crashes, gas spikes, and rate divergence every cycle

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

# AI-powered swap recommendation (quote only)
python -m src swap --ai

# Full autonomous demo (dry run)
python demo_swap.py

# Full autonomous demo (live on-chain)
python demo_swap.py --live --ai
```

## CLI Commands

| Command | Description |
|---|---|
| `scan` | Live USDC yield rates across protocols |
| `allocate` | Optimal allocation plan with risk analysis |
| `execute` | Execute allocation (paper mode by default) |
| `swap` | Uniswap swap with AI reasoning (`--ai` flag) |
| `swap --ai --live` | AI-decided live on-chain swap |
| `run` | Continuous agent loop (scan + allocate + execute) |
| `health` | System health check (circuit breakers + protocol health) |
| `dashboard` | Audit trail with P&L summary and yield curve |
| `portfolio` | Current portfolio state from database |
| `history` | Execution history log |
| `emergency-withdraw` | Instant full withdrawal (bypasses cooldowns) |
| `register` | Register on ERC-8004 Identity Registry |

## Uniswap Integration

### Trading API

Full integration with the [Uniswap Trading API](https://docs.uniswap.org/api/trading-api/overview) for token swaps on Base:

- **Permit2 flow** — EIP-712 typed data signing with domain validation
- **Optimal routing** — Automatic best-price routing across V2/V3/V4 pools and UniswapX
- **Swap target allowlist** — Only known Uniswap contracts accepted (Universal Router, Permit2)
- **Gas price ceiling** — Prevents overspend on gas spikes
- **AI-powered decisions** — Claude Haiku analyzes market data and recommends swap direction/amount

### AI Swap Reasoning

The agent uses Claude AI (with rule-based fallback) to decide:
- **When** to swap (market timing based on yield rates vs ETH appreciation)
- **What direction** (USDC -> WETH for ETH exposure, WETH -> USDC for yield)
- **How much** (amount bounded by safety caps — max 50% of any balance per swap)

```
  Wallet State + Yield Rates + Gas Costs
                    ↓
          ┌─────────────────┐
          │   Claude Haiku  │  (or rule-based fallback)
          │   AI Reasoning  │
          └────────┬────────┘
                   ↓
    ┌──────────────────────────────┐
    │  Structured Recommendation   │
    │  action: swap_weth_to_usdc   │
    │  amount: $25.00              │
    │  confidence: 85%             │
    │  reasoning: "Lock in gains"  │
    └──────────────────────────────┘
                   ↓
         Safety Bounds Check
         (max 50%, min $1, balance cap)
                   ↓
        Execute via Uniswap API
```

Safety: No private keys pass through the AI. Only public market data is analyzed. AI output is parsed as strict JSON with bounds enforcement.

### On-Chain Transactions (Base Mainnet)

| Action | Tx Hash | Details |
|---|---|---|
| USDC -> WETH swap | `0xd368dae5...` | 1 USDC via Uniswap Trading API |
| ETH -> USDC swap | `0x15192308...` | 0.002 ETH -> 4.60 USDC (block 43446422) |
| Aave V3 deposit #1 | `0xdd2dcabb...` | 10 USDC supply (block 43440158) |
| Aave V3 deposit #2 | `0x638f4567...` | 4 USDC supply (block 43446450) |
| **Total Aave position** | — | **14 USDC earning ~2.5% APY** |

**Wallet:** `0x8d691720bF8C81044DB1a77b82D0eF5f5bffdE6C`

### ERC-8004 Agent Identity

Registered as **Agent #32272** on the ERC-8004 Identity Registry (Base mainnet). Declared capabilities: yield scanning, risk assessment, portfolio management, safety monitoring.

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
| ETH price | CoinGecko | Used for WETH valuation |

### Strategy Engine

- **Risk scoring** — 5-factor weighted score (TVL 25%, age 20%, audits 20%, utilization 20%, bad debt 15%)
- **Net APY** — Gross APY minus amortized gas costs over expected hold period
- **Risk-adjusted yield** — `net_apy * (1 - risk_score)` — protocols ranked by this
- **Allocation** — Proportional to risk-adjusted yield, with per-protocol caps

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
| Max swap per action | 50% of balance |

**Circuit breakers (run every cycle):**

| Breaker | Threshold | Action |
|---|---|---|
| USDC depeg | > 0.5% from $1.00 | Emergency withdraw ALL |
| TVL crash | > 30% drop in 1h | Emergency withdraw protocol |
| Gas freeze | > 200 gwei | Freeze all moves |
| Rate divergence | > 2% between sources | Pause protocol |

### Protocols

| Protocol | Chain | Status |
|---|---|---|
| Aave V3 | Base | Active |
| Morpho Blue (MetaMorpho) | Base | Active |
| Compound V3 | Base | Monitored |
| Uniswap (V2/V3/V4/UniswapX) | Base | Swap routing |

## Security

**8 security audits completed** — all findings fixed:

| Audit | Findings | Fixed |
|---|---|---|
| #1 — Data layer | 9 (2 CRIT, 3 HIGH) | All |
| #2 — Strategy engine | 7 (3 CRIT, 4 HIGH) | All |
| #3 — Execution engine | 3 HIGH + 6 MEDIUM | All |
| #4 — Circuit breakers | 2 HIGH + 3 MEDIUM | All |
| #5 — Full codebase | 1 HIGH + 2 MEDIUM | All |
| #6 — Depeg monitor | 3 findings | All |
| #7 — Uniswap adapter | 4 HIGH + 6 MEDIUM | All |
| #8 — AI swap module | Bounds enforcement | Pass |

Key security measures:
- Private key isolated via `TransactionSigner` — never stored in config, never sent to AI
- Chain ID validated at startup + enforced per transaction
- Swap target allowlist — only known Uniswap contracts
- Permit2 domain validation (chain ID + verifying contract)
- AI output parsed as strict JSON with safety bounds (max 50% swap, amount caps)
- Gas price ceiling prevents overspend
- Depeg price validated against [$0.50, $1.50] bounds

## Testing

```bash
pytest tests/           # 214 tests
pytest tests/ -v        # Verbose output
```

**214 tests** covering data layer, protocols, strategy, execution, portfolio, circuit breakers, health monitor, security, Uniswap adapter, and AI swap reasoning.

## Configuration

All runtime config in `config/default.yaml`. Environment variables (`.env`):

| Variable | Purpose |
|---|---|
| `BASE_RPC_URL` | Base chain RPC endpoint |
| `PRIVATE_KEY` | Wallet private key (live mode only) |
| `UNISWAP_API_KEY` | Uniswap Trading API key |
| `ANTHROPIC_API_KEY` | Claude AI for swap reasoning (optional — rule-based fallback) |

## Tech Stack

- **Python 3.13** — agent logic
- **web3.py** — on-chain contract reads + transaction execution
- **Anthropic SDK** — Claude AI for swap reasoning
- **Uniswap Trading API** — token swap execution with Permit2
- **aiohttp** — async API calls (DeFi Llama, CoinGecko, Uniswap)
- **SQLite** (aiosqlite) — portfolio state, execution log, audit trail
- **Click** — CLI framework

## Project Structure

```
src/
├── main.py              # CLI entry point (11 commands)
├── config.py            # YAML config + spending scope validation
├── models.py            # Data models (16 dataclasses/enums)
├── database.py          # SQLite persistence
├── executor.py          # Paper/dry-run/live execution engine
├── portfolio.py         # Position tracking + yield accrual
├── uniswap.py           # Uniswap Trading API adapter (Permit2, routing)
├── ai_swap.py           # AI-powered swap reasoning (Claude + fallback)
├── erc8004.py           # ERC-8004 agent identity registration
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

demo.py                  # Agent lifecycle demo (scan -> allocate -> execute)
demo_swap.py             # Autonomous swap + yield demo (AI reasoning)
```

## Demos

### `demo.py` — Agent Lifecycle
Demonstrates the standard yield agent flow: scan rates, health check, allocate, paper execute, dashboard.

### `demo_swap.py` — Autonomous Swap + Yield Loop
Demonstrates the full AI-powered DeFi agent:
1. Check wallet balances (USDC + WETH)
2. AI analyzes yield rates and recommends action
3. Execute Uniswap swap if recommended
4. Deposit USDC into highest-yield protocol
5. Report final state with tx receipts

```bash
python demo_swap.py              # Dry run
python demo_swap.py --live       # Live execution
python demo_swap.py --live --ai  # AI-powered live execution
```

## Related Projects

- **[synthesis-zk-agent](https://github.com/SenorCodigo69/synthesis-zk-agent)** — ZK privacy layer (Track 2: "Agents that keep secrets") with Uniswap V4 ZK-gated hook
- **Conversation log:** [`CONVERSATION-LOG.md`](https://github.com/SenorCodigo69/finance_agent/blob/main/docs/hackathon/CONVERSATION-LOG.md)

## Hackathon Details

- **Event:** Ethereum Foundation Synthesis Hackathon
- **Tracks:** "Agents that pay" + Uniswap "Agentic Finance" bounty
- **Building period:** March 13-22, 2026
- **Primary AI:** Claude Opus 4.6 via claude-code
- **On-chain:** Base mainnet (chain ID 8453)

## License

MIT
