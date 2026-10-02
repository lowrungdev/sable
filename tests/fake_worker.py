"""A scriptable stand-in for sable.plugin_host, for testing the core's side of the protocol.

Run as ``python -I fake_worker.py <entry path>``. It speaks the wire protocol of
``sable.plugins`` and misbehaves on request, so that the core can be tested
against hangs, crashes, garbage and floods without the real worker.

What it does is decided by data the test controls:

* the ``settings`` of the ``load`` message: ``declare`` (the declaration to
  answer with), ``load`` (how loading goes: ok, hang, garbage, error, crash),
  ``check`` (absent, ``ok``, ``reject`` or ``hang``);
* the first word of a call's ``args``: ``echo``, ``hang``, ``crash`` and the rest,
  listed in ``COMMANDS`` below.

Standard library only. Not collected by pytest (the name does not start with
``test_``).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

OUT = os.fdopen(os.dup(1), "wb")
LOCK = threading.Lock()
WAITING: dict[str, dict] = {}
EVENTS: dict[str, threading.Event] = {}
STATE = {"settings": {}, "plugin": "", "inflight": 0, "max_inflight": 0, "seq": 0}
STATE_LOCK = threading.Lock()


def send(obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":")).encode() + b"\n"
    with LOCK:
        OUT.write(line)
        OUT.flush()


def send_raw(data: bytes) -> None:
    with LOCK:
        OUT.write(data)
        OUT.flush()


def act(call: int, action: str, **args) -> dict:
    """Send an act and wait for the core's answer."""
    with STATE_LOCK:
        STATE["seq"] += 1
        act_id = f"a{STATE['seq']}"
    event = threading.Event()
    EVENTS[act_id] = event
    send({"op": "act", "id": act_id, "call": call, "action": action, "args": args})
    event.wait(10)
    return WAITING.pop(act_id, {"ok": False, "error": "no answer"})


def default_declaration(settings: dict) -> dict:
    flip = settings.get("flip")
    if flip:
        # Declare one thing the first time this plugin is loaded and another after:
        # what a restart that changes the plugin looks like.
        again = os.path.exists(flip)
        open(flip, "w").close()
        if again:
            return settings["declare_after"]
    declared = settings.get("declare")
    if declared is not None:
        return declared
    return {
        "commands": [{"name": "hi", "aliases": ["yo"], "help": "say hi", "usage": "hi <text>"}],
        "phrases": [],
        "schedules": [],
        "has_check": "check" in settings,
    }


def on_load(msg: dict) -> None:
    settings = msg.get("settings", {})
    STATE["settings"] = settings
    STATE["plugin"] = msg.get("plugin", "")
    mode = settings.get("load", "ok")
    if mode == "hang":
        time.sleep(3600)
    elif mode == "garbage":
        send_raw(b"this is not json\n")
    elif mode == "error":
        send({"op": "error", "id": msg["id"], "error": "import failed: no module named nothing"})
    elif mode == "error-leak":
        send({"op": "error", "id": msg["id"], "error": "bad key " + settings["api_key"]})
    elif mode == "crash":
        os._exit(7)
    elif mode == "stderr-then-ok":
        sys.stderr.write("loading now\n")
        sys.stderr.flush()
        send({"op": "loaded", "id": msg["id"], "declared": default_declaration(settings)})
    else:
        send({"op": "loaded", "id": msg["id"], "declared": default_declaration(settings)})


def on_check(msg: dict) -> None:
    mode = STATE["settings"].get("check", "ok")
    if mode == "hang":
        time.sleep(3600)
    elif mode == "reject":
        send({"op": "result", "id": msg["id"], "ok": False, "error": "the api url is wrong"})
    else:
        send({"op": "result", "id": msg["id"], "ok": True})


def result(rid: int, reply: str | None = None, **extra) -> None:
    send({"op": "result", "id": rid, "ok": True, "reply": reply, **extra})


def on_phrase_call(rid: int, ctx: dict) -> None:
    """A phrase handler. What it does is the ``phrase`` setting for its handler id:
    ``reply`` (the default), ``none``, ``ctx``, ``mention``, ``crash``, ``hang``,
    ``error`` (a PluginError), ``leak`` (a PluginError quoting a setting and an
    embedded newline, as a hostile or buggy handler might) or ``exception``."""
    behaviour = STATE["settings"].get("phrase", {}).get(ctx["name"], "reply")
    if behaviour == "none":
        result(rid, None)
    elif behaviour == "ctx":
        result(rid, json.dumps(ctx))
    elif behaviour == "mention":
        result(rid, "@all heads up")
    elif behaviour == "crash":
        os._exit(3)
    elif behaviour == "hang":
        time.sleep(3600)
    elif behaviour == "error":
        send({"op": "result", "id": rid, "ok": False, "error": "no thanks", "user_visible": True})
    elif behaviour == "leak":
        key = STATE["settings"]["api_key"]
        text = f"bad key {key}\nFAKE LOG LINE: pwned"
        send({"op": "result", "id": rid, "ok": False, "error": text, "user_visible": True})
    elif behaviour == "exception":
        send(
            {
                "op": "result",
                "id": rid,
                "ok": False,
                "error": "ValueError: boom",
                "user_visible": False,
            }
        )
    else:
        result(rid, f"{STATE['plugin']}/{ctx['name']} matched {ctx['match']}")


def on_schedule_call(rid: int, ctx: dict) -> None:
    """A schedule handler. What it does is the ``schedule`` setting for its handler
    id: ``reply`` (the default: a string naming the plugin/handler/room),
    ``none``, ``ctx``, ``crash``, ``hang``, ``error`` (a PluginError),
    ``exception``, ``send`` (an explicit ``ctx.send`` into its own room, as an
    action rather than a return value), ``reply-action`` (an explicit
    ``ctx.reply``, which should behave the same as ``send`` for a schedule) or
    ``react`` (tries ``ctx.react``, which should always be refused: a schedule has
    no triggering message)."""
    behaviour = STATE["settings"].get("schedule", {}).get(ctx["name"], "reply")
    if behaviour == "none":
        result(rid, None)
    elif behaviour == "ctx":
        result(rid, json.dumps(ctx))
    elif behaviour == "crash":
        os._exit(3)
    elif behaviour == "hang":
        time.sleep(3600)
    elif behaviour == "error":
        send({"op": "result", "id": rid, "ok": False, "error": "no thanks", "user_visible": True})
    elif behaviour == "exception":
        send(
            {
                "op": "result",
                "id": rid,
                "ok": False,
                "error": "ValueError: boom",
                "user_visible": False,
            }
        )
    elif behaviour == "send":
        answer = act(rid, "send", room=ctx["room"], text=f"sent to {ctx['room']}")
        result(rid, json.dumps(answer))
    elif behaviour == "send-foreign":
        target = STATE["settings"].get("foreign_room", "zzzz9999")
        answer = act(rid, "send", room=target, text="sneaky")
        result(rid, json.dumps(answer))
    elif behaviour == "reply-action":
        answer = act(rid, "reply", text=f"replied in {ctx['room']}")
        result(rid, json.dumps(answer))
    elif behaviour == "react":
        answer = act(rid, "react", emoji="\U0001f44d")
        result(rid, json.dumps(answer))
    elif behaviour == "sleep":
        time.sleep(float(STATE["settings"].get("sleep_seconds", 0.2)))
        result(rid, f"slept in {ctx['room']}; max in flight {STATE['max_inflight']}")
    elif behaviour == "fail-for-one":
        if ctx["room"] == STATE["settings"].get("fail_room"):
            send({"op": "result", "id": rid, "ok": False, "error": "nope", "user_visible": True})
        else:
            result(rid, f"ok in {ctx['room']}")
    else:
        result(rid, f"{STATE['plugin']}/{ctx['name']} fired in {ctx['room']}")


def on_call(msg: dict) -> None:
    rid = msg["id"]
    ctx = msg["ctx"]
    trigger = ctx.get("trigger")
    # Tracked for every kind of call (not just commands): a schedule's fan-out is
    # exactly what test_at_most_four_schedule_calls_are_in_flight reads this for.
    with STATE_LOCK:
        STATE["inflight"] += 1
        STATE["max_inflight"] = max(STATE["max_inflight"], STATE["inflight"])
    try:
        if trigger == "phrase":
            on_phrase_call(rid, ctx)
        elif trigger == "schedule":
            on_schedule_call(rid, ctx)
        else:
            args = ctx.get("args", "")
            word, _, rest = args.partition(" ")
            COMMANDS.get(word, unknown)(rid, rest, ctx)
    finally:
        with STATE_LOCK:
            STATE["inflight"] -= 1


def unknown(rid: int, rest: str, ctx: dict) -> None:
    send({"op": "result", "id": rid, "ok": False, "error": "unknown", "user_visible": False})


def c_echo(rid, rest, ctx):
    result(rid, rest)


def c_none(rid, rest, ctx):
    result(rid, None)


def c_hang(rid, rest, ctx):
    time.sleep(3600)


def c_sleep(rid, rest, ctx):
    time.sleep(float(rest or "0.2"))
    result(rid, f"slept; max in flight {STATE['max_inflight']}")


def c_crash(rid, rest, ctx):
    os._exit(3)


def c_garbage(rid, rest, ctx):
    send_raw(b"{this is not json\n")


def c_notobject(rid, rest, ctx):
    send_raw(b"[1,2,3]\n")


def c_badop(rid, rest, ctx):
    send({"op": "weird", "id": rid})


def c_oversize(rid, rest, ctx):
    send_raw(b"x" * (2 * 1024 * 1024) + b"\n")


def c_unknownid(rid, rest, ctx):
    send({"op": "result", "id": 987654, "ok": True, "reply": "nobody asked"})


def c_lateact(rid, rest, ctx):
    result(rid, "done")
    send({"op": "act", "id": "late1", "call": rid, "action": "reply", "args": {"text": "late"}})


def c_acts(rid, rest, ctx):
    """Reply N times, then answer with how many were accepted."""
    count = int(rest or "1")
    ok = sum(1 for i in range(count) if act(rid, "reply", text=f"part {i}").get("ok"))
    result(rid, f"{ok} of {count} accepted")


def c_flood(rid, rest, ctx):
    """Fire acts without waiting for any answer."""
    for i in range(int(rest or "1000")):
        send({"op": "act", "id": f"f{i}", "call": rid, "action": "reply", "args": {"text": "x"}})
    time.sleep(3600)


def c_send(rid, rest, ctx):
    room, _, text = rest.partition(" ")
    answer = act(rid, "send", room=room, text=text)
    result(rid, json.dumps(answer))


def c_react(rid, rest, ctx):
    answer = act(rid, "react", emoji=rest)
    result(rid, json.dumps(answer))


def c_badaction(rid, rest, ctx):
    answer = act(rid, "teleport", where="mars")
    result(rid, json.dumps(answer))


def c_emptyreply(rid, rest, ctx):
    answer = act(rid, "reply", text="   ")
    result(rid, json.dumps(answer))


def c_silent(rid, rest, ctx):
    answer = act(rid, "reply", text="shh", silent=True)
    result(rid, None, acted=answer.get("ok"))


def c_cwd(rid, rest, ctx):
    result(rid, os.getcwd())


def c_session(rid, rest, ctx):
    result(rid, json.dumps({"pid": os.getpid(), "sid": os.getsid(0), "pgid": os.getpgid(0)}))


def c_stderrblob(rid, rest, ctx):
    sys.stderr.write("B" * 3_000_000 + "\n")
    sys.stderr.flush()
    result(rid, "wrote")


def c_leak(rid, rest, ctx):
    """A crash whose message quotes a setting, as a plugin's own exception may."""
    key = STATE["settings"]["api_key"]
    sys.stderr.write(f"Traceback: RuntimeError: bad key {key}\n")
    sys.stderr.flush()
    send(
        {
            "op": "result",
            "id": rid,
            "ok": False,
            "error": f"RuntimeError: bad key {key}",
            "user_visible": False,
        }
    )


def c_sigterm(rid, rest, ctx):
    import signal

    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(10)


def c_stop(rid, rest, ctx):
    """Freeze: never read stdin again."""
    import signal

    os.kill(os.getpid(), signal.SIGSTOP)


def c_orphan(rid, rest, ctx):
    """Exit, leaving a child in a session of its own that keeps stdout open."""
    pid = os.fork()
    if pid == 0:
        os.setsid()
        # Keep stdout (the protocol pipe) but let go of stderr, so that only the
        # pipe the core reads from is held.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        time.sleep(float(rest or "3"))
        os._exit(0)
    time.sleep(0.05)
    os._exit(5)


def c_dupresult(rid, rest, ctx):
    """Answer twice: the second is for a call that is already over."""
    result(rid, "first")
    result(rid, "second")


def c_nlflood(rid, rest, ctx):
    sys.stderr.write("\n" * 3_000_000)
    sys.stderr.flush()
    result(rid, "wrote")


def c_huge(rid, rest, ctx):
    result(rid, "y" * int(rest or "100000"))


def c_bigacts(rid, rest, ctx):
    """Four replies of 9000 characters against a budget of 20,000: the third is cut, the
    fourth refused."""
    answers = [act(rid, "reply", text="z" * 9000) for _ in range(4)]
    result(rid, json.dumps([a.get("ok") for a in answers]))


def c_mention(rid, rest, ctx):
    result(rid, "@all heads up")


def c_env(rid, rest, ctx):
    result(rid, json.dumps(dict(os.environ)))


def c_settings(rid, rest, ctx):
    result(rid, json.dumps(STATE["settings"]))


def c_ctx(rid, rest, ctx):
    result(rid, json.dumps(ctx))


def c_pluginerror(rid, rest, ctx):
    send({"op": "result", "id": rid, "ok": False, "error": rest, "user_visible": True})


def c_exception(rid, rest, ctx):
    sys.stderr.write("Traceback (most recent call last):\n  boom\n")
    sys.stderr.flush()
    send(
        {"op": "result", "id": rid, "ok": False, "error": "ValueError: boom", "user_visible": False}
    )


def c_stderr(rid, rest, ctx):
    for i in range(int(rest or "10")):
        sys.stderr.write(f"line {i} \x1b[31mred\x1b[0m\n")
    sys.stderr.write("L" * 5000 + "\n")
    sys.stderr.flush()
    result(rid, "wrote")


def c_pid(rid, rest, ctx):
    result(rid, str(os.getpid()))


def c_child(rid, rest, ctx):
    """Start a background child in this process group and report its pid."""
    pid = os.fork()
    if pid == 0:
        time.sleep(3600)
        os._exit(0)
    result(rid, str(pid))


def c_proc(rid, rest, ctx):
    """Try to read the parent's environment and memory through /proc."""
    ppid = os.getppid()
    outcome = {}
    try:
        with open(f"/proc/{ppid}/environ", "rb") as handle:
            data = handle.read(1 << 20)
        outcome["environ"] = {"read": len(data), "secret": b"TOPSECRET" in data}
    except OSError as exc:
        outcome["environ"] = {"denied": type(exc).__name__}
    try:
        outcome["mem"] = {"read": read_parent_memory(ppid)}
    except OSError as exc:
        outcome["mem"] = {"denied": type(exc).__name__}
    except LookupError as exc:
        outcome["mem"] = {"nothing": str(exc)}
    result(rid, json.dumps(outcome))


def read_parent_memory(ppid: int) -> int:
    """Read bytes out of the parent's address space: open its mappings, then its memory.

    Opening /proc/<pid>/mem is not the test: that succeeds or fails on the flags alone
    on some kernels. A read that returns data is.
    """
    with open(f"/proc/{ppid}/maps") as maps:
        regions = [line.split() for line in maps]
    for fields in regions:
        low = fields[0].partition("-")[0]
        if not fields[1].startswith("rw") or fields[-1].startswith(("[vvar", "[vsyscall")):
            continue
        try:
            with open(f"/proc/{ppid}/mem", "rb", buffering=0) as mem:
                mem.seek(int(low, 16))
                data = mem.read(16)
        except OSError as exc:
            if isinstance(exc, PermissionError):
                raise
            continue
        if data:
            return len(data)
    raise LookupError("no readable region")


def c_limits(rid, rest, ctx):
    import resource

    names = ("RLIMIT_AS", "RLIMIT_NOFILE", "RLIMIT_CORE", "RLIMIT_CPU")
    result(rid, json.dumps({n: resource.getrlimit(getattr(resource, n)) for n in names}))


COMMANDS = {
    name[2:]: func for name, func in globals().items() if name.startswith("c_") and callable(func)
}


def main() -> None:
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return
        msg = json.loads(line)
        op = msg.get("op")
        if op == "shutdown":
            return
        if op == "load":
            on_load(msg)
        elif op == "check":
            threading.Thread(target=on_check, args=(msg,), daemon=True).start()
        elif op == "call":
            threading.Thread(target=on_call, args=(msg,), daemon=True).start()
        elif op == "act_result":
            WAITING[msg["id"]] = msg
            event = EVENTS.get(msg["id"])
            if event is not None:
                event.set()


if __name__ == "__main__":
    main()
