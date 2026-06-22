# Rate limiter for the public API
Decision: FIXED-WINDOW counter, 1000 req/min per API key, one Redis key with
INCR then EXPIRE. Simple and fast.
