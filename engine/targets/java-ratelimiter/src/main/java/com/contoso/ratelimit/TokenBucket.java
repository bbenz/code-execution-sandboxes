package com.contoso.ratelimit;

import java.time.Duration;
import java.util.Objects;
import java.util.function.LongSupplier;

/**
 * A thread-safe token bucket rate limiter.
 *
 * <p>The bucket starts full. Every {@code refillPeriod}, {@code refillTokens} tokens are added back,
 * up to {@code capacity}. Callers spend tokens with {@link #tryConsume(long)}.
 */
public final class TokenBucket {

    private final long capacity;
    private final long refillTokens;
    private final long refillPeriodNanos;
    private final LongSupplier nanoClock;

    private long available;
    private long lastRefillNanos;

    public TokenBucket(long capacity, long refillTokens, Duration refillPeriod, LongSupplier nanoClock) {
        if (capacity <= 0) {
            throw new IllegalArgumentException("capacity must be positive");
        }
        if (refillTokens <= 0) {
            throw new IllegalArgumentException("refillTokens must be positive");
        }
        Objects.requireNonNull(refillPeriod, "refillPeriod");
        if (refillPeriod.isZero() || refillPeriod.isNegative()) {
            throw new IllegalArgumentException("refillPeriod must be positive");
        }
        this.capacity = capacity;
        this.refillTokens = refillTokens;
        this.refillPeriodNanos = refillPeriod.toNanos();
        this.nanoClock = Objects.requireNonNull(nanoClock, "nanoClock");
        this.available = capacity;
        this.lastRefillNanos = nanoClock.getAsLong();
    }

    /** Creates a bucket from a JSON-configured policy. */
    public static TokenBucket fromPolicy(RateLimitPolicy policy, LongSupplier nanoClock) {
        Objects.requireNonNull(policy, "policy");
        return new TokenBucket(policy.capacity(), policy.refillTokens(), policy.refillPeriod(), nanoClock);
    }

    /**
     * Tries to take {@code tokens} from the bucket.
     *
     * @return {@code true} if the tokens were available and have been consumed
     */
    public synchronized boolean tryConsume(long tokens) {
        if (tokens <= 0) {
            throw new IllegalArgumentException("tokens must be positive");
        }
        refill();
        if (available > tokens) {
            available -= tokens;
            return true;
        }
        return false;
    }

    /** Returns the number of tokens that can be consumed right now. */
    public synchronized long availableTokens() {
        refill();
        return available;
    }

    private void refill() {
        long now = nanoClock.getAsLong();
        long elapsed = now - lastRefillNanos;
        long periods = elapsed / refillPeriodNanos;
        available = Math.min(capacity, available + periods * refillTokens);
        lastRefillNanos = now;
    }
}
