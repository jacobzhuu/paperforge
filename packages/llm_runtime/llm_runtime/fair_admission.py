"""Atomic, least-occupied-job admission sharing the existing provider lease set."""

# KEYS: existing leases, waiting expiry, FIFO order, waiting metadata, lease owners,
# last grant by job, sequence. ARGV: token, total cap, job id, priority (0 foreground).
ACQUIRE = r"""
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)
for _,token in ipairs(expired) do
 redis.call('HDEL', KEYS[5], token)
 redis.call('HDEL', KEYS[9], token)
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
for _,token in ipairs(redis.call('HKEYS', KEYS[5])) do
 if not redis.call('ZSCORE', KEYS[1], token) then redis.call('HDEL', KEYS[5], token) end
end
for _,token in ipairs(redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)) do
 redis.call('ZREM', KEYS[2], token)
 redis.call('ZREM', KEYS[3], token)
 redis.call('HDEL', KEYS[4], token)
 redis.call('HDEL', KEYS[8], token)
end
if not redis.call('ZSCORE', KEYS[3], ARGV[1]) then
 local seq = redis.call('INCR', KEYS[7])
 redis.call('ZADD', KEYS[3], seq, ARGV[1])
 redis.call('HSET', KEYS[4], ARGV[1], cjson.encode({ARGV[3], tonumber(ARGV[4])}))
end
redis.call('HSETNX', KEYS[8], ARGV[1], now)
redis.call('ZADD', KEYS[2], now + 120000, ARGV[1])
for i=2,10 do redis.call('PEXPIRE', KEYS[i], 180000) end
local counts = {}
local background = 0
local cap = tonumber(ARGV[2])
local backgroundcap = math.max(1, math.min(2, cap - 1))
local streak = tonumber(redis.call('GET', KEYS[10]) or '0')
for _,token in ipairs(redis.call('ZRANGE', KEYS[1], 0, -1)) do
 local job = redis.call('HGET', KEYS[5], token)
 if job then counts[job] = (counts[job] or 0) + 1 end
 if redis.call('HGET', KEYS[9], token) == '1' then background = background + 1 end
end
local seen = {}
local best, bestjob, bestpriority, bestcount, bestlast = nil, nil, 2, math.huge, math.huge
for _,token in ipairs(redis.call('ZRANGE', KEYS[3], 0, -1)) do
 local raw = redis.call('HGET', KEYS[4], token)
 if raw then
  local m = cjson.decode(raw)
  local job, priority = m[1], m[2]
  local since = tonumber(redis.call('HGET', KEYS[8], token) or now)
  local eligible = priority == 0 or (background < backgroundcap and (counts[job] or 0) < 2)
  if priority > 0 and background == 0 and now - since >= 60000
     and (cap > 1 or streak >= 5) then priority = -1 end
  if not seen[job] and eligible then
   seen[job] = true
   local count = counts[job] or 0
   local last = tonumber(redis.call('HGET', KEYS[6], job) or '0')
   if priority < bestpriority or (priority == bestpriority and
      (count < bestcount or (count == bestcount and last < bestlast))) then
    best, bestjob, bestpriority, bestcount, bestlast = token, job, priority, count, last
   end
  end
 end
end
for _,job in ipairs(redis.call('HKEYS', KEYS[6])) do
 if not seen[job] and not counts[job] then redis.call('HDEL', KEYS[6], job) end
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) or best ~= ARGV[1]
 or (tonumber(ARGV[4]) > 0 and (counts[ARGV[3]] or 0) >= 2) then
 return {0, counts[ARGV[3]] or 0, redis.call('ZCARD', KEYS[1]), redis.call('ZCARD', KEYS[2])}
end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('HDEL', KEYS[4], ARGV[1])
redis.call('HDEL', KEYS[8], ARGV[1])
redis.call('HSET', KEYS[9], ARGV[1], ARGV[4])
redis.call('PEXPIRE', KEYS[9], 180000)
if tonumber(ARGV[4]) > 0 then redis.call('SET', KEYS[10], 0, 'PX', 180000)
else redis.call('INCR', KEYS[10]); redis.call('PEXPIRE', KEYS[10], 180000) end
redis.call('ZADD', KEYS[1], now + 120000, ARGV[1])
redis.call('PEXPIRE', KEYS[1], 180000)
redis.call('HSET', KEYS[5], ARGV[1], bestjob)
redis.call('HSET', KEYS[6], bestjob, redis.call('INCR', KEYS[7]))
return {1, bestcount + 1, redis.call('ZCARD', KEYS[1]),
 redis.call('ZCARD', KEYS[2]), bestpriority == -1 and 1 or 0}
"""

RELEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('HDEL', KEYS[4], ARGV[1])
redis.call('HDEL', KEYS[5], ARGV[1])
redis.call('HDEL', KEYS[8], ARGV[1])
redis.call('HDEL', KEYS[9], ARGV[1])
return 1
"""


def keys(provider_key):
    return [provider_key] + [
        provider_key + ":fair:" + suffix
        for suffix in (
            "waiting",
            "order",
            "metadata",
            "owners",
            "last",
            "sequence",
            "waiting_since",
            "lease_priorities",
            "foreground_streak",
        )
    ]
