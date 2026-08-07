from rate_limit import RateLimiter


def test_cooldown_blocks_second_immediate_call():
    rl = RateLimiter(cooldown_s=10, per_user_per_min=100, global_per_min=100)
    ok, _ = rl.check(1)
    assert ok
    rl.record(1)
    ok, reason = rl.check(1)
    assert not ok and "chill" in reason


def test_per_user_cap():
    rl = RateLimiter(cooldown_s=0, per_user_per_min=3, global_per_min=100)
    for _ in range(3):
        assert rl.check(1)[0]
        rl.record(1)
    assert rl.check(1)[0] is False


def test_global_cap():
    rl = RateLimiter(cooldown_s=0, per_user_per_min=100, global_per_min=2)
    for u in (1, 2):
        assert rl.check(u)[0]
        rl.record(u)
    assert rl.check(3)[0] is False
