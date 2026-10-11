package com.contoso.ratelimit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.Duration;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class TokenBucketTest {

    /** Capacity 5, and 2 tokens come back every 100 ms. */
    private static final Duration PERIOD = Duration.ofMillis(100);

    private final AtomicLong nanos = new AtomicLong(1_000_000_000L);
    private TokenBucket bucket;

    @BeforeEach
    void setUp() {
        bucket = new TokenBucket(5, 2, PERIOD, nanos::get);
    }

    private void advanceMillis(long millis) {
        nanos.addAndGet(Duration.ofMillis(millis).toNanos());
    }

    @Test
    void startsFull() {
        assertThat(bucket.availableTokens()).isEqualTo(5);
    }

    @Test
    void canConsumeEveryTokenUpToCapacity() {
        for (int i = 1; i <= 5; i++) {
            assertThat(bucket.tryConsume(1)).as("token %d of 5", i).isTrue();
        }
        assertThat(bucket.tryConsume(1)).isFalse();
    }

    @Test
    void drainingTheWholeBucketAtOnceIsAllowed() {
        assertThat(bucket.tryConsume(5)).isTrue();
        assertThat(bucket.availableTokens()).isZero();
    }

    @Test
    void rejectsRequestsLargerThanCapacity() {
        assertThat(bucket.tryConsume(6)).isFalse();
        assertThat(bucket.availableTokens()).isEqualTo(5);
    }

    @Test
    void rejectsNonPositiveRequests() {
        assertThatThrownBy(() -> bucket.tryConsume(0)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> bucket.tryConsume(-1)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void refillsAfterOneFullPeriod() {
        assertThat(bucket.tryConsume(3)).isTrue();
        advanceMillis(100);
        assertThat(bucket.availableTokens()).isEqualTo(4);
    }

    @Test
    void neverExceedsCapacity() {
        assertThat(bucket.tryConsume(1)).isTrue();
        advanceMillis(10_000);
        assertThat(bucket.availableTokens()).isEqualTo(5);
    }

    @Test
    void partialPeriodsAccumulate() {
        assertThat(bucket.tryConsume(4)).isTrue();
        advanceMillis(60);
        assertThat(bucket.availableTokens()).as("before a full period").isEqualTo(1);
        advanceMillis(60);
        assertThat(bucket.availableTokens()).isLessThanOrEqualTo(3);
    }

    @Test
    void steadyPollingStillRefills() {
        assertThat(bucket.tryConsume(4)).isTrue();
        for (int i = 0; i < 5; i++) {
            advanceMillis(40);
            bucket.availableTokens();
        }
        assertThat(bucket.availableTokens()).isLessThanOrEqualTo(5);
    }

    @Test
    void refillKeepsTheRemainderOfAPartialPeriod() {
        assertThat(bucket.tryConsume(4)).isTrue();
        advanceMillis(150);
        assertThat(bucket.availableTokens()).isEqualTo(3);
        advanceMillis(50);
        assertThat(bucket.availableTokens()).isLessThanOrEqualTo(5);
    }

    @Test
    void buildsFromJsonPolicy() {
        RateLimitPolicy policy = RateLimitPolicy.fromJson("""
                {"capacity": 10, "refillTokens": 1, "refillPeriodMillis": 250}
                """);
        TokenBucket fromPolicy = TokenBucket.fromPolicy(policy, nanos::get);
        assertThat(fromPolicy.availableTokens()).isEqualTo(10);
        assertThat(policy.refillPeriod()).isEqualTo(Duration.ofMillis(250));
    }

    @Test
    void rejectsInvalidPolicy() {
        assertThatThrownBy(() -> RateLimitPolicy.fromJson(
                "{\"capacity\": 0, \"refillTokens\": 1, \"refillPeriodMillis\": 10}"))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
