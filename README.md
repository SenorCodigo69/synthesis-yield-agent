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
| `pools` | Uniswap pool analytics (fee APY, TVL, IL risk) |
| `lp` | LP position management (mint, collect, exit) |
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
| LP Position (full-range) | NFT #4816034 | WETH-USDC 0.05% pool |

**Wallet:** `0x8d691720bF8C81044DB1a77b82D0eF5f5bffdE6C`

### ERC-8004 Agent Identity

Registered as **Agent #32272** on the ERC-8004 Identity Registry (Base mainnet). Declared capabilities: yield scanning, risk assessment, portfolio management, safety monitoring.

## Uniswap LP (Liquidity Provision)

Full-range LP position management for WETH-USDC on Base, with AI-driven concentrated liquidity optimization.

### LP Commands

```bash
# Basic position management
python -m src lp --action status --token-id 123              # Read position state
python -m src lp --action mint --weth 1.0 --usdc 2500        # Full-range mint (dry-run)
python -m src lp --action mint --weth 1.0 --usdc 2500 --live # Full-range mint (on-chain)
python -m src lp --action collect --token-id 123 --live      # Collect fees
python -m src lp --action exit --token-id 123 --live         # Full exit (3 txs)

# AI-driven concentrated LP
python -m src lp --action optimize                           # Show quant signals + recommended tick range
python -m src lp --action concentrated-mint --weth 0.5 --usdc 1000 --live  # Mint with optimized range
python -m src lp --action rebalance --token-id 123           # Check if rebalance needed
python -m src lp --action il-report --token-id 123           # Impermanent loss + fee profitability
```

### Concentrated LP (AI-Driven)

The agent uses quant signals from the trading engine to compute optimal tick ranges for concentrated liquidity positions, maximizing fee capture while minimizing impermanent loss.

**Signal pipeline** (fully on-chain, zero external APIs):
1. Read ETH price from WETH-USDC pool `slot0().sqrtPriceX96`
2. Store snapshots in SQLite → build OHLC candles from history
3. Compute ATR, Bollinger Bands, RSI, ADX from candles
4. Detect market regime (BULL/BEAR/SIDEWAYS) via weighted-vote system

**Tick range optimizer** (8-step pipeline):
1. ATR-based width (2x ATR = ~95% daily coverage)
2. Regime adjustment (SIDEWAYS → tight, BULL → wide+skew up, BEAR → wide+skew down or exit)
3. ADX gate (strong trend → widen 50%)
4. RSI extremes (overbought → skew down, oversold → skew up)
5. Bollinger clamp (range vs BB sanity check)
6. Safety bounds (5% minimum, 50% maximum)
7. Price bounds → tick conversion
8. Tick alignment to fee tier spacing

**Automated LP Manager** (`lp_manager.py`):
- Continuous loop: read pool → compute signals → mint/hold/rebalance/exit
- Auto-rebalance triggers: out-of-range, edge proximity, regime change, staleness
- Bear protection: exits LP during strong bear, stays out until regime shifts
- Rebalance execution: exit old position → mint new with updated range
- Configurable interval (default 5min), graceful stop

**Rebalance triggers:**
| Trigger | Urgency | Action |
|---|---|---|
| Out of range (earning 0 fees) | HIGH | Always rebalance |
| Near edge (within 10%) | MEDIUM | Rebalance if gas OK |
| Regime change | LOW | Rebalance if gas OK |
| Stale (>24h) | LOW | Rebalance if gas OK |

**IL Tracker:** Concentrated IL formula + fee-vs-IL profitability. Reports whether LP is outperforming HODL.

**Learning Loop** (`lp_learner.py`):
- Tracks win/loss outcomes per regime (BULL/BEAR/SIDEWAYS)
- Records every decision (mint, rebalance, exit) with regime context
- Feeds back into optimizer: adjusts width multipliers based on historical performance
- SQLite persistence — survives restarts, accumulates over time

### LP Features

- **Full-range + concentrated positions** — mint, collect fees, exit, auto-rebalance
- **4 fee tiers** — 0.01%, 0.05% (default), 0.3%, 1%
- **On-chain signals** — ATR, Bollinger Bands, RSI, ADX from pool reads (no CoinGecko)
- **Regime detection** — BULL/BEAR/SIDEWAYS classification from ported trading agent logic
- **Tick math** — price ↔ tick conversions for WETH-USDC (18/6 decimals), float-safe bounds
- **Safety** — gas ceiling (5 gwei), slippage protection, deadline enforcement, nonce tracking

### Pool Analytics

```bash
python -m src pools                # USDC-paired pool analytics
python -m src pools --all-pairs    # All pairs
python -m src pools --limit 20     # Top 20
```

Fetches Uniswap V3/V2 pool data from DeFi Llama — fee APY, TVL, IL risk. Used in AI swap reasoning to compare LP yield vs lending yield.

## Execution Logger ("Let the Agent Cook")

Machine-readable audit trail for the [Protocol Labs / EF bounty](https://synthesis.md/):

- **`agent.json`** — capability manifest declaring tools, safety guardrails, and autonomous loop
- **`src/execution_logger.py`** — structured JSON logger capturing every cycle: steps, tool calls, decisions (with reasoning), executions (with tx hashes), failures, compute budget
- **Output:** `data/agent_log.json` — bounded to 500 cycles, corrupt file recovery, safe serialization
- **Read API:** `get_recent_cycles(n)` and `get_stats()` for dashboard/submission

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
| Lido stETH Treasury | Ethereum | Active — [companion repo](https://github.com/SenorCodigo69/synthesis-steth-treasury) |

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
pytest tests/           # 371 tests
pytest tests/ -v        # Verbose output
```

**371 tests** covering data layer, protocols, strategy, execution, portfolio, circuit breakers, health monitor, security, Uniswap adapter, AI swap reasoning, LP management, pool analytics, tick math, concentrated LP optimizer, rebalancer, IL tracker, LP manager, learning loop, and execution logger.

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
├── execution_logger.py  # Structured JSON execution logger (agent.json companion)
├── uniswap_lp.py        # LP position management (full-range + concentrated mint)
├── lp_signals.py        # On-chain pool reads → snapshots → indicators + regime
├── lp_tick_math.py      # Tick ↔ price conversions for WETH-USDC
├── lp_optimizer.py      # Regime-aware tick range optimizer (8-step pipeline)
├── lp_rebalancer.py     # Rebalance trigger detection (OOR, edge, regime, stale)
├── lp_il_tracker.py     # Concentrated LP impermanent loss + fee profitability
├── lp_manager.py        # Automated LP loop (mint → monitor → rebalance → exit)
├── lp_learner.py        # Learning loop — tracks outcomes per regime, adjusts optimizer
├── uniswap_skills.py    # Uniswap ecosystem compatibility layer
├── circuit_breakers.py  # Depeg, TVL crash, gas freeze, rate divergence
├── health_monitor.py    # Pre-execution health checks (6 per protocol)
├── depeg_monitor.py     # Live USDC price fetching + validation
├── data/
│   ├── aggregator.py    # Cross-validation engine
│   ├── defillama.py     # DeFi Llama API client
│   ├── gas.py           # Gas price tracking
│   ├── onchain.py       # Direct contract reads (Aave, Compound)
│   └── uniswap_pools.py # Uniswap pool analytics (fee APY, TVL, IL risk)
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

agent.json               # Machine-readable capability manifest
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

- **[synthesis-steth-treasury](https://github.com/SenorCodigo69/synthesis-steth-treasury)** — stETH Agent Treasury (Lido bounty) — yield-bearing operating budget on Ethereum mainnet, agent can only spend accrued staking yield
- **[synthesis-zk-agent](https://github.com/SenorCodigo69/synthesis-zk-agent)** — ZK privacy layer (Track 2: "Agents that keep secrets") with Uniswap V4 ZK-gated hook
- **Conversation log:** [`CONVERSATION-LOG.md`](https://github.com/SenorCodigo69/finance_agent/blob/main/docs/hackathon/CONVERSATION-LOG.md)

## Hackathon Details

- **Event:** Ethereum Foundation Synthesis Hackathon
- **Tracks:** "Agents that pay" + Uniswap "Agentic Finance" bounty + Lido "stETH Agent Treasury" bounty
- **Building period:** March 13-22, 2026
- **Primary AI:** Claude Opus 4.6 via claude-code
- **On-chain:** Base mainnet (chain ID 8453)

## License

MIT
