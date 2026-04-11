# knarr-thrall — moved

This subdirectory previously held the **knarr-thrall** plugin source. The
plugin was spun out to its own dedicated repository on 2026-03-23 and the
canonical source is now:

> **<https://github.com/knarrnet/knarr.thrall>**

A 2026-03-23 → 2026-04-10 split-brain period left some work landing in this
subdirectory after the spin-out had already happened. That work was reconciled
back into the canonical repository on 2026-04-11 and tagged as **v3.11.1** on
`knarrnet/knarr.thrall`. After the reconciliation completed, the duplicate
files in this subdirectory were removed in favor of this pointer file to
prevent the situation from recurring.

## Where to find things now

| Looking for | Now lives at |
|---|---|
| Plugin source code | <https://github.com/knarrnet/knarr.thrall> |
| Plugin issues / CRs | <https://github.com/knarrnet/knarr.thrall/issues> |
| Plugin releases | <https://github.com/knarrnet/knarr.thrall/releases> |
| Historical reviews and release notes prior to v3.11.1 | `F:/thing/reviews/thrall-historical/` (operator-private) |

## For deployments

If you previously deployed thrall by symlinking or copying from
`knarr.skills/guard/knarr-thrall/`, switch to cloning `knarr.thrall` directly:

```bash
git clone https://github.com/knarrnet/knarr.thrall.git
ln -s "$PWD/knarr.thrall" /path/to/knarr-node/plugins/06-thrall
```

Or pin a specific release:

```bash
git clone https://github.com/knarrnet/knarr.thrall.git
cd knarr.thrall
git checkout v3.11.1
```

## Why this directory still exists

A stub README rather than a fully removed path so that anyone with stale
clones, scripts, or documentation pointing at `knarr.skills/guard/knarr-thrall/`
gets a clear "moved" pointer rather than a missing directory error.
