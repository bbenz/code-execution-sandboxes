package com.contoso.ratelimit;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.Duration;

/** Rate limit settings, typically loaded from JSON configuration. */
public record RateLimitPolicy(long capacity, long refillTokens, long refillPeriodMillis) {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    public RateLimitPolicy {
        if (capacity <= 0 || refillTokens <= 0 || refillPeriodMillis <= 0) {
            throw new IllegalArgumentException(
                    "capacity, refillTokens, and refillPeriodMillis must all be positive");
        }
    }

    /** Parses a policy such as {@code {"capacity": 10, "refillTokens": 1, "refillPeriodMillis": 250}}. */
    public static RateLimitPolicy fromJson(String json) {
        try {
            return MAPPER.readValue(json, RateLimitPolicy.class);
        } catch (JsonProcessingException e) {
            throw new IllegalArgumentException("Invalid rate limit policy: " + e.getOriginalMessage(), e);
        }
    }

    public Duration refillPeriod() {
        return Duration.ofMillis(refillPeriodMillis);
    }
}
