"""A fake AI client for P3. No network, no key, no SDK.

The real client wraps the anthropic SDK Batch API and has the same three methods. A Request
(built by the tool) has: custom_id, model, system, text, max_tokens. A result is
crawler.ai.BatchResult(custom_id, output, raw_text, input_tokens, output_tokens), with
cache_read_tokens and cache_write_tokens defaulting to 0.

Batches finish at once: batch_results() returns the list on the first ask.
"""
import json


class Truncated(str):
    """Return this from a responder: the answer was cut off at max_tokens (billed, output is None)."""


class BatchGone(Exception):
    """What the real client raises when a batch no longer exists (HTTP 404)."""
    status_code = 404


class FakeAI:
    gone_ids = ()

    def __init__(self, responder, tokens=None, usage=None):
        self.gone_ids = set()  # batch ids that answer 404 when asked for results
        """
        responder(request) -> dict (structured output) or str (unusable text, billed anyway)
        tokens: None (len(system+text)//4), an int, or callable(model, system, text) -> int
        usage(request, counted_tokens) -> (input_tokens, output_tokens) actually billed;
                default is (counted_tokens, 100)
        """
        self.responder = responder
        self.tokens = tokens
        self.usage = usage
        self.calls = []      # ("count_tokens" | "submit_batch" | "batch_results", ...) in order
        self.requests = []   # every request ever submitted, in order
        self.batches = {}

    def _count(self, model, system, text):
        if callable(self.tokens):
            return self.tokens(model, system, text)
        if self.tokens is not None:
            return self.tokens
        return (len(system) + len(text)) // 4

    def count_tokens(self, model, system, text):
        self.calls.append(("count_tokens", model))
        return self._count(model, system, text)

    def submit_batch(self, requests):
        from crawler.ai import BatchResult
        self.calls.append(("submit_batch", len(requests)))
        batch_id = f"batch_{len(self.batches) + 1}"
        results = []
        for r in requests:
            self.requests.append(r)
            answer = self.responder(r)
            counted = self._count(r.model, r.system, r.text)
            in_tok, out_tok = self.usage(r, counted) if self.usage else (counted, 100)
            if isinstance(answer, dict):
                output, raw = answer, json.dumps(answer)
            else:
                output, raw = None, str(answer)
            results.append(BatchResult(custom_id=r.custom_id, output=output, raw_text=raw,
                                       input_tokens=in_tok, output_tokens=out_tok,
                                       truncated=isinstance(answer, Truncated)))
        self.batches[batch_id] = results
        return batch_id

    def batch_results(self, batch_id):
        self.calls.append(("batch_results", batch_id))
        if batch_id in self.gone_ids:
            raise BatchGone(f"batch {batch_id} not found")
        return self.batches[batch_id]

    def submitted_requests(self):
        return len(self.requests)


class FakeDiscoverAI:
    """Fake of the discovery client: one live web-search call per brand.

    The real client has the same two methods:
      supports_web_search(model) -> bool           (asks the Models API)
      discover(model, brand, max_uses, max_tokens) -> crawler.ai.DiscoverReply
    A DiscoverReply has output (dict or None), raw_text, input_tokens, output_tokens, searches,
    and optional cache_read_tokens / cache_write_tokens (default 0) and truncated (default False).
    """

    def __init__(self, output, *, web_search_models=("claude-haiku-5-5", "claude-sonnet-5-5"),
                 input_tokens=3000, output_tokens=500, searches=5, truncated=False):
        self.output = output
        self.web_search_models = set(web_search_models)
        self.usage = (input_tokens, output_tokens, searches)
        self.truncated = truncated
        self.calls = []  # ("supports_web_search", model) and ("discover", model, brand, max_uses, max_tokens)

    def supports_web_search(self, model):
        self.calls.append(("supports_web_search", model))
        return model in self.web_search_models

    def discover(self, model, brand, max_uses, max_tokens):
        from crawler.ai import DiscoverReply
        self.calls.append(("discover", model, brand, max_uses, max_tokens))
        in_tok, out_tok, searches = self.usage
        out = self.output(brand) if callable(self.output) else self.output
        if isinstance(out, dict):
            output, raw = out, json.dumps(out)
        else:
            output, raw = None, str(out)
        return DiscoverReply(output=output, raw_text=raw, input_tokens=in_tok, output_tokens=out_tok,
                             searches=searches, truncated=self.truncated)

    def discover_calls(self):
        return [c for c in self.calls if c[0] == "discover"]


class FakeGapAI:
    """Fake for gap-fill: one web search per model family, limited to the brand's approved hosts.

    The real client method is
      find_documents(model, brand, query, hosts, max_uses, max_tokens) -> crawler.ai.DiscoverReply
    whose output is {"documents": [{"url": ..., "evidence_url": ...}]}; supports_web_search(model) as for discovery.
    `documents` is a dict, or callable(query) -> dict. The default finds nothing (a miss).
    """

    def __init__(self, documents=None, *, web_search_models=("claude-haiku-5-5", "claude-sonnet-5-5"),
                 input_tokens=2000, output_tokens=300, searches=1):
        self.documents = documents if documents is not None else {"documents": []}
        self.web_search_models = set(web_search_models)
        self.usage = (input_tokens, output_tokens, searches)
        self.calls = []

    def supports_web_search(self, model):
        self.calls.append(("supports_web_search", model))
        return model in self.web_search_models

    def find_documents(self, model, brand, query, hosts, max_uses, max_tokens):
        from crawler.ai import DiscoverReply
        self.calls.append(("find_documents", model, brand, query, tuple(hosts), max_uses, max_tokens))
        in_tok, out_tok, searches = self.usage
        out = self.documents(query) if callable(self.documents) else self.documents
        return DiscoverReply(output=out, raw_text=json.dumps(out), input_tokens=in_tok, output_tokens=out_tok,
                             searches=searches)

    def searches(self):
        return [c for c in self.calls if c[0] == "find_documents"]


class FakeLinkAI:
    """Fake for the "Should I crawl this page?" check: one live call per page carrying all its unsure links.

    The real client method is
      ask_links(model, context, links, max_tokens) -> crawler.ai.DiscoverReply (searches = 0)
    context is {"url", "title", "heading"}; links is [(url, link_text), ...], numbered from 1 in the request.
    The output is {"answers": [{"n": 1, "crawl": true, "reason": "..."}], "suggest_skip": ["news", ...]}.
    `answer` is callable(url, text) -> bool, or a ready output dict / unusable string returned as is.
    """

    def __init__(self, answer=lambda url, text: True, *, suggest=(), input_tokens=400, output_tokens=60):
        self.answer = answer
        self.suggest = list(suggest)
        self.usage = (input_tokens, output_tokens)
        self.calls = []

    def ask_links(self, model, context, links, max_tokens):
        from crawler.ai import DiscoverReply
        self.calls.append(("ask_links", model, dict(context), list(links), max_tokens))
        in_tok, out_tok = self.usage
        if callable(self.answer):
            out = {"answers": [{"n": n, "crawl": bool(self.answer(u, t)), "reason": "fake reason"}
                               for n, (u, t) in enumerate(links, start=1)], "suggest_skip": self.suggest}
        else:
            out = self.answer
        if isinstance(out, dict):
            return DiscoverReply(output=out, raw_text=json.dumps(out), input_tokens=in_tok, output_tokens=out_tok, searches=0)
        return DiscoverReply(output=None, raw_text=str(out), input_tokens=in_tok, output_tokens=out_tok, searches=0)
