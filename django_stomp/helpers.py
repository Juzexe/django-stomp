import functools
import time


def slow_down(_func=None, *args, **kwargs):
    """Sleep given amount of seconds before calling the function"""

    before = kwargs.get("before", 0.2)
    after = kwargs.get("after", 0.2)

    def decorator_slow_down(func):
        @functools.wraps(func)
        def wrapper_slow_down(*args, **kwargs):
            time.sleep(before)
            value = func(*args, **kwargs)
            time.sleep(after)
            return value

        return wrapper_slow_down

    if _func is None:
        return decorator_slow_down
    else:
        return decorator_slow_down(_func)
