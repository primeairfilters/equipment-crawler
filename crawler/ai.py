"""AI pieces: the price table, the monthly spend, and the real client (a thin wrapper on the Batch API).

The key is read from the environment only (ANTHROPIC_API_KEY_CRAWLER) and is never printed or logged.
"""
import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"

# dollars per million tokens: (input, output). Batch calls cost half.
PRICES = {
    HAIKU: (Decimal("0.10"), Decimal("0.50")),
    SONNET: (Decimal("2"), Decimal("10")),
}
SEARCH_PRICE = Decimal("0.01")
MILLION = Decimal(1_000_000)


class UnknownModel(Exception):
    pass


@dataclass
class Request:
    custom_id: str
    model: str
    system: str
    text: str
    max_tokens: int


@dataclass
class BatchResult:
    custom_id: str
    output: object
    raw_text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    truncated: bool = False
    errored: bool = False  # the request failed, expired or was canceled: not billed


@dataclass
class DiscoverReply:
    output: object
    raw_text: str
    input_tokens: int
    output_tokens: int
    searches: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    truncated: bool = False


LONG_PROMPT_TOKENS = 100_000
LONG_PROMPT_MULTIPLIER = 5  # Haiku 5.5 bills 5x above 100k prompt tokens


def cost(model, input_tokens=0, output_tokens=0, batch=False, searches=0, check_long=True):
    """Dollars. An unknown model is an error: there is no fallback price."""
    if model not in PRICES:
        raise UnknownModel(f"no price for model {model!r}")
    price_in, price_out = PRICES[model]
    total = (Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out) / MILLION
    if check_long and model == HAIKU and input_tokens > LONG_PROMPT_TOKENS:
        total = total * LONG_PROMPT_MULTIPLIER
    if batch:
        total = total / 2
    return total + SEARCH_PRICE * searches


def monthly_spend(conn, now):
    """Everything logged in ai_calls during the calendar month of `now`."""
    start = datetime(now.year, now.month, 1)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_calls WHERE created_at >= %s AND created_at < %s",
                    (start, end))
        return Decimal(cur.fetchone()[0])


class AnthropicClient:
    """The three calls extraction needs. Batches finish later; batch_results returns None until then."""

    def __init__(self, api_key=None):
        import anthropic
        key = api_key or os.environ.get("ANTHROPIC_API_KEY_CRAWLER")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY_CRAWLER is not set.")
        self._sdk = anthropic.Anthropic(api_key=key)

    @staticmethod
    def _system(system):
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def count_tokens(self, model, system, text):
        reply = self._sdk.messages.count_tokens(
            model=model, system=self._system(system), messages=[{"role": "user", "content": text}])
        return reply.input_tokens

    def supports_web_search(self, model):
        info = self._sdk.models.retrieve(model)
        return bool(info.capabilities and info.capabilities.server_tools.web_search.supported)

    def discover(self, model, brand, max_uses, max_tokens):
        """One live call with the web search tool (not a batch). The answer is a JSON object."""
        from crawler.discover import DISCOVER_SCHEMA, DISCOVER_SYSTEM
        reply = self._sdk.messages.create(
            model=model, max_tokens=max_tokens, system=DISCOVER_SYSTEM,
            messages=[{"role": "user", "content": f"Brand: {brand}"}],
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses,
                    "allowed_callers": ["direct"]}],
            output_config={"format": {"type": "json_schema", "schema": DISCOVER_SCHEMA}})
        return self._search_reply(reply)

    def find_documents(self, model, brand, query, hosts, max_uses, max_tokens):
        """One live web search, limited to the given hosts. The answer is {"documents": [{url, evidence_url}]}."""
        from crawler.gapfill import GAP_SCHEMA, GAP_SYSTEM
        reply = self._sdk.messages.create(
            model=model, max_tokens=max_tokens, system=GAP_SYSTEM,
            messages=[{"role": "user", "content": f"Brand: {brand}\nFind: {query}"}],
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses,
                    "allowed_domains": list(hosts), "allowed_callers": ["direct"]}],
            output_config={"format": {"type": "json_schema", "schema": GAP_SCHEMA}})
        return self._search_reply(reply)

    @staticmethod
    def _search_reply(reply):
        texts = [b.text for b in reply.content if getattr(b, "type", "") == "text"]
        raw = texts[-1] if texts else ""
        try:
            output = json.loads(raw)
        except ValueError:
            output = None
        usage = reply.usage
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        wrote = getattr(usage, "cache_creation_input_tokens", 0) or 0
        tool_use = getattr(usage, "server_tool_use", None)
        searches = getattr(tool_use, "web_search_requests", 0) or 0
        usable = reply.stop_reason == "end_turn" and isinstance(output, dict)
        return DiscoverReply(output if usable else None, raw, usage.input_tokens + read + -(-wrote * 5 // 4),
                             usage.output_tokens, searches, read, wrote, truncated=reply.stop_reason == "max_tokens")

    def submit_batch(self, requests):
        from crawler.extract import OUTPUT_SCHEMA
        batch = self._sdk.messages.batches.create(requests=[
            {"custom_id": r.custom_id,
             "params": {"model": r.model, "max_tokens": r.max_tokens, "system": self._system(r.system),
                        "messages": [{"role": "user", "content": r.text}],
                        "thinking": {"type": "disabled"},
                        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}}}
            for r in requests])
        return batch.id

    def batch_results(self, batch_id):
        batch = self._sdk.messages.batches.retrieve(batch_id)
        if batch.processing_status != "ended":
            return None
        results = []
        for item in self._sdk.messages.batches.results(batch_id):
            if item.result.type != "succeeded":
                results.append(BatchResult(item.custom_id, None, "", 0, 0, errored=True))
                continue
            message = item.result.message
            raw = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
            try:
                output = json.loads(raw)
            except ValueError:
                output = None
            usage = message.usage
            read = getattr(usage, "cache_read_input_tokens", 0) or 0
            wrote = getattr(usage, "cache_creation_input_tokens", 0) or 0
            # cache reads count as ordinary input and cache writes at 1.25x, so the logged cost is never too low
            results.append(BatchResult(item.custom_id, output if isinstance(output, dict) else None, raw,
                                       usage.input_tokens + read + -(-wrote * 5 // 4), usage.output_tokens, read, wrote,
                                       truncated=message.stop_reason == "max_tokens"))
        return results
