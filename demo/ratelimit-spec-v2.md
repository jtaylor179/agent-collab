# Rate limiter for the public API (v2)
Decision: TOKEN BUCKET, 1000 tokens/min refill, per API key, implemented as a single
atomic Redis Lua script (read+refill+decrement). No INCR/EXPIRE race.
