"""Demo project — live agent viewer check."""

from datetime import datetime, timezone


def greet(name: str = "world") -> str:
    return f"hey {name} — prompt-capture verify @ {datetime.now(timezone.utc):%H:%M:%S}Z"


def status() -> dict[str, object]:
    return {
        "agent": "adrian",
        "room": "274VK3",
        "feature": "workspace-walkup-prompt",
        "ok": True,
        "note": "should attach Cursor prompt this time",
    }


def fib(n: int) -> list[int]:
    """Tiny helper so the diff is obvious on the board."""
    seq = [0, 1]
    while len(seq) < max(n, 2):
        seq.append(seq[-1] + seq[-2])
    return seq[:n]


def shout(msg: str) -> str:
    return f"! {msg.upper()} !"


if __name__ == "__main__":
    print(greet("hackday"))
    print(status())
    print("fib(10) =", fib(10))
    print(shout("prompt walkup online"))
