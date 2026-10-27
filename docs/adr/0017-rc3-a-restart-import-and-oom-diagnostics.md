# ADR 0017 — RC3-A trusted restart fixture resolution and OOM evidence

Status: accepted for test-harness implementation; release and image baseline NOT approved.

Remote C/40510798314 and M/40516191942 both fail the original SQLite/PostgreSQL
restart IDs because the fixture hardcodes /app PYTHONPATH outside that checkout.
Resolve test-only imports from the checked-in helper's own absolute location, with
fixed source paths, regular-file/link checks, fixed cwd/absolute child script, and
an environment allowlist. Do not inherit PYTHONPATH, host credentials or accept root
from fixture JSON/Agent. Verify import origins; persist selected source SHA-256 and
origin in the existing post-commit/SIGKILL marker and compare to the current parent.
Preserve -SIGKILL, restart uncertainty, quota/budget retention, original Test IDs,
and exact-owned finally cleanup. Portability and negative contracts are additive.

This helper is trusted test harness code, not an Agent runtime or production guard.
No runtime/fencing/lease/outcome changes, new permissions, schema migration or image
recipe change. Update versioned source/case manifest transparently, retain all1582
required IDs and175 original Windows skip IDs/reasons, never bless skip/failures.

Remote M PostgreSQL OOM has exit137 but OOMKilled=false and unknown_execution.
137 alone cannot establish cgroup OOM or safe replay. Preserve UNKNOWN fail-closed.
Fresh bounded diagnostics may inspect only newly bound disposable test identities;
cgroup memory limits/peak/events and daemon State must be distinguished from missing
historical Runner/kernel evidence. A new synthetic probe is not a replay of the
unknown business Run and cannot retroactively prove its root cause. Record missing
proof as UNKNOWN; do not approve image baselines or extend leases to hide failures.
