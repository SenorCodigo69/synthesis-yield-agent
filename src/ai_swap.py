"""AI-powered swap reasoning — Claude decides when and what to swap.

Uses Claude Haiku to analyze yield rates, wallet balances, and market
context, then recommends swap actions (USDC <-> WETH) or yield deposits.

This is the "brain" of the agentic finance loop:
  1. Observe: yield rates, balances, gas costs
  2. Reason: Claude analyzes the data and decides the optimal action
  3. Act: execute the recommended swap or deposit

Security:
- No private keys pass through the AI — only public market data
- AI output is parsed as structured JSON with strict validation
- Invalid/unexpected recommendations are rejected (fail-closed)
- Amount bounds enforced regardless of AI output
"""

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

logger = logging.getLogger(__name__)

# Maximum fraction of balance the AI can recommend swapping
MAX_SWAP_FRACTION = Decimal("0.5")  # Never swap more than 50% at once
MIN_SWAP_USD = Decimal("1")  # Don't swap dust


class SwapAction(str, Enum):
    SWAP_USDC_TO_WETH = "swap_usdc_to_weth"
    SWAP_WETH_TO_USDC = "swap_weth_to_usdc"
    DEPOSIT_YIELD = "deposit_yield"
    HOLD = "hold"


@dataclass
class SwapRecommendation:
    """Structured swap recommendation from the AI."""
    action: SwapAction
    amount_usd: Decimal
    reasoning: str
    confidence: float  # 0.0 - 1.0


def build_analysis_prompt(
    usdc_balance: Decimal,
    weth_balance_usd: Decimal,
    yield_rates: list[dict],
    gas_gwei: Decimal,
    eth_price: Decimal,
) -> str:
    """Build the analysis prompt for Claude."""
    rates_text = ""
    for r in yield_rates:
        rates_text += (
            f"  - {r['protocol']}: {r['apy']:.2%} APY, "
            f"${r['tvl']:,.0f} TVL, {r['utilization']:.1%} util\n"
        )

    return f"""You are an autonomous DeFi yield agent on Base chain. Analyze the current state and recommend ONE action.

## Current State
- USDC balance: ${usdc_balance:,.2f}
- WETH balance: ${weth_balance_usd:,.2f} (valued in USD)
- ETH price: ${eth_price:,.2f}
- Gas: {gas_gwei:.4f} gwei (Base L2 — very cheap)

## Yield Rates (USDC lending on Base)
{rates_text}
## Rules
1. Primary goal: maximize risk-adjusted yield on idle USDC
2. Only swap USDC to WETH if you have strong conviction ETH will appreciate more than lending yield
3. Swap WETH back to USDC if you can lock in gains and deposit for yield
4. Never recommend swapping more than 50% of any balance
5. If balances are small (<$5), prefer HOLD to avoid gas waste
6. Depositing into the highest-yielding protocol is usually the right move for idle USDC

## Output Format
Respond with ONLY a JSON object (no markdown, no explanation outside JSON):
{{
  "action": "swap_usdc_to_weth" | "swap_weth_to_usdc" | "deposit_yield" | "hold",
  "amount_usd": <number>,
  "reasoning": "<1-2 sentences>",
  "confidence": <0.0-1.0>
}}"""


def parse_recommendation(
    response_text: str,
    usdc_balance: Decimal,
    weth_balance_usd: Decimal,
) -> SwapRecommendation:
    """Parse and validate AI response into a SwapRecommendation.

    Enforces safety bounds regardless of what the AI recommends.
    """
    # Strip markdown fences if present
    text = response_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"AI response not valid JSON: {e}")
        return SwapRecommendation(
            action=SwapAction.HOLD,
            amount_usd=Decimal("0"),
            reasoning="Failed to parse AI response — defaulting to HOLD",
            confidence=0.0,
        )

    # Parse action
    action_str = data.get("action", "hold")
    try:
        action = SwapAction(action_str)
    except ValueError:
        logger.warning(f"Unknown action: {action_str}")
        return SwapRecommendation(
            action=SwapAction.HOLD,
            amount_usd=Decimal("0"),
            reasoning=f"Unknown action '{action_str}' — defaulting to HOLD",
            confidence=0.0,
        )

    # Parse amount with bounds enforcement
    try:
        raw_amount = Decimal(str(data.get("amount_usd", 0)))
    except (InvalidOperation, TypeError):
        raw_amount = Decimal("0")

    # Enforce safety bounds
    if action == SwapAction.SWAP_USDC_TO_WETH:
        max_allowed = usdc_balance * MAX_SWAP_FRACTION
        amount = min(raw_amount, max_allowed)
    elif action == SwapAction.SWAP_WETH_TO_USDC:
        max_allowed = weth_balance_usd * MAX_SWAP_FRACTION
        amount = min(raw_amount, max_allowed)
    elif action == SwapAction.DEPOSIT_YIELD:
        amount = min(raw_amount, usdc_balance)
    else:
        amount = Decimal("0")

    # Floor at zero
    amount = max(amount, Decimal("0"))

    # Skip dust amounts
    if amount < MIN_SWAP_USD and action != SwapAction.HOLD:
        return SwapRecommendation(
            action=SwapAction.HOLD,
            amount_usd=Decimal("0"),
            reasoning=f"Amount ${amount:.2f} below minimum — HOLD",
            confidence=0.0,
        )

    confidence = data.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)):
        confidence = 0.5
    confidence = max(0.0, min(1.0, float(confidence)))

    reasoning = str(data.get("reasoning", "No reasoning provided"))[:500]

    return SwapRecommendation(
        action=action,
        amount_usd=amount,
        reasoning=reasoning,
        confidence=confidence,
    )


async def get_swap_recommendation(
    usdc_balance: Decimal,
    weth_balance_usd: Decimal,
    yield_rates: list[dict],
    gas_gwei: Decimal,
    eth_price: Decimal,
    anthropic_api_key: str | None = None,
) -> SwapRecommendation:
    """Get AI-powered swap recommendation from Claude.

    Falls back to rule-based logic if API key is not set or call fails.
    """
    # Rule-based fallback if no API key
    if not anthropic_api_key:
        return _rule_based_recommendation(
            usdc_balance, weth_balance_usd, yield_rates, eth_price,
        )

    prompt = build_analysis_prompt(
        usdc_balance, weth_balance_usd, yield_rates, gas_gwei, eth_price,
    )

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=anthropic_api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text
        logger.info(f"AI recommendation: {response_text[:200]}")

        return parse_recommendation(response_text, usdc_balance, weth_balance_usd)

    except ImportError:
        logger.warning("anthropic package not installed — using rule-based fallback")
        return _rule_based_recommendation(
            usdc_balance, weth_balance_usd, yield_rates, eth_price,
        )
    except Exception as e:
        logger.warning(f"AI call failed: {e} — using rule-based fallback")
        return _rule_based_recommendation(
            usdc_balance, weth_balance_usd, yield_rates, eth_price,
        )


def _rule_based_recommendation(
    usdc_balance: Decimal,
    weth_balance_usd: Decimal,
    yield_rates: list[dict],
    eth_price: Decimal,
) -> SwapRecommendation:
    """Simple rule-based fallback when AI is unavailable.

    Strategy: if we have WETH, swap back to USDC and deposit for yield.
    If we have idle USDC, deposit into the best-yielding protocol.
    """
    # If we have WETH, swap it back to USDC for yield
    if weth_balance_usd > MIN_SWAP_USD:
        return SwapRecommendation(
            action=SwapAction.SWAP_WETH_TO_USDC,
            amount_usd=weth_balance_usd * MAX_SWAP_FRACTION,
            reasoning=(
                f"WETH balance ${weth_balance_usd:.2f} can be swapped to USDC "
                f"for yield. Best rate: {_best_apy(yield_rates):.2%}"
            ),
            confidence=0.7,
        )

    # If we have idle USDC, deposit for yield
    if usdc_balance > MIN_SWAP_USD:
        best = _best_apy(yield_rates)
        if best > Decimal("0.01"):  # >1% APY worth depositing
            return SwapRecommendation(
                action=SwapAction.DEPOSIT_YIELD,
                amount_usd=usdc_balance * Decimal("0.8"),  # Keep 20% reserve
                reasoning=f"Idle USDC available. Best yield: {best:.2%} APY",
                confidence=0.8,
            )

    return SwapRecommendation(
        action=SwapAction.HOLD,
        amount_usd=Decimal("0"),
        reasoning="No profitable action identified",
        confidence=0.9,
    )


def _best_apy(rates: list[dict]) -> Decimal:
    """Extract the best APY from yield rates."""
    if not rates:
        return Decimal("0")
    return max(Decimal(str(r.get("apy", 0))) for r in rates)
