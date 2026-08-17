# Transcripts

198 model exchanges, one JSON file per call, keyed by `sha256(prompt_version + request payload)`.

Each file holds the exact request sent and the response received, which is what makes a re-run reproduce byte-identically: the cache is replayed rather than the model re-queried. Temperature zero does not guarantee that; a content-addressed cache does.

API keys never appear here — entries are written through `config.redact`.
