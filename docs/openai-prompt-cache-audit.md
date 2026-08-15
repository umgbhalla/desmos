# OpenAI multi-turn prompt-cache audit

Date: 2026-08-15  
Endpoint class: ChatGPT/Codex OAuth Responses API  
Model: `gpt-5.6-sol`

## Live probe

A credential-safe probe used one stable instruction prefix, a first step that
completed one Desmos syscall across two model turns, and a second conversational
step over the same transcript. Prompts, credentials, and reasoning content were
not recorded.

| Provider turn | Uncached input | Cached input | Total input | Cache hit |
|---:|---:|---:|---:|---:|
| 1 (cold) | 4,873 | 0 | 4,873 | 0.0% |
| 2 (after tool result) | 1,074 | 3,840 | 4,914 | 78.1% |
| 3 (next user turn) | 1,292 | 3,840 | 5,132 | 74.8% |

The two warm turns read 7,680 of 10,046 input tokens from cache: **76.4%**.
The stable 3,840-token prefix remained cached across both the tool-result turn
and the following user turn.

## Findings

- Multi-turn tool use preserves a cacheable prefix. A completed syscall result
  did not invalidate the prior prefix.
- The 2,366 uncached warm tokens are the growing dynamic suffix across the two
  turns. The provider metrics do not identify any of them as avoidable waste.
- The cold turn's 4,873 tokens are a one-time fill cost inferred from subsequent
  reads. Responses usage does not report cache-write tokens.
- Desmos therefore maps provider `cached_tokens` to cache reads but leaves
  `cache_creation_input_tokens` at zero; that zero means **unreported**, not a
  measured absence of writes.
- No request change is recommended. The OAuth Codex path intentionally omits
  `prompt_cache_key`: the existing live A/B evidence in `desmos/openai.py`
  shows that sending the key on this endpoint caused a miss, while omitting it
  produced an automatic cache hit. API-key requests still receive the key.

## Code paths

- Request construction and typed syscall tool: `desmos/openai.py:253-289`
- Provider usage mapping: `desmos/openai.py:351-365`
- Endpoint-specific cache-key behavior: `desmos/openai.py:594-605`

## Conclusion

OpenAI prompt caching is working across real multi-turn Desmos tool calls.
Warm-prefix reuse is provider-reported and independently observed on two
successive turns. Cache-write volume and write waste cannot be measured from
the Responses usage fields currently returned by the OAuth endpoint.
