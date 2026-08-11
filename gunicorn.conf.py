# Gunicorn picks this file up automatically from the working directory, so it
# needs no change to the Render start command.
#
# Why it exists: with the default single sync worker, Aurora answers one guest
# at a time. Measured live — twelve people asking at once took 81 seconds, i.e.
# perfectly serial, with the last person waiting over a minute. Every one of
# those seconds is spent waiting on the Anthropic API, not on CPU, so threads
# fix it without needing a bigger instance.
#
# One worker, several threads: the app keeps its state (conversations, phone
# bindings) in module-level dicts, which are shared inside a process but NOT
# across processes. Adding workers instead of threads would split that state
# and make Aurora forget people at random. Threads keep one shared copy.

workers = 1
threads = 8
worker_class = "gthread"

# A reply needs an Anthropic round trip; the default 30s timeout can kill a slow
# one mid-flight and leave the guest with silence.
timeout = 120
graceful_timeout = 30
keepalive = 5
