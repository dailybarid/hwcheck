# tools/ — build-machine tooling

Nothing in here runs on the USB stick. These are the scripts used to *produce*
the kit. They are kept in the repository for two reasons: so the i18n
migration is reproducible, and so nobody accidentally publishes a report
containing a real machine's serial numbers.

## check_clean.py — the publishing gate

```bash
python3 tools/check_clean.py
```

A hardware-inspection tool leaks by design: its own output contains serial
numbers, BIOS versions, MAC addresses, disk models and battery data from
whatever laptop it was last run on. This scans every committed file for
absolute home paths, MAC and IPv4 addresses, e-mail addresses, DMI
fingerprints and serial assignments.

**Run it before every push.** Add your own machine's fingerprints to the
`FORBIDDEN` list at the top of the file.

`report/`, `iso/`, `ventoy/` and `*.deb` are excluded — they are either test
output or large binaries, and `.gitignore` keeps them out of the repository
anyway.

## check_translations.py — catalog validation

```bash
python3 tools/check_translations.py          # all languages
python3 tools/check_translations.py --lang fr
```

Fails on a missing or invented key, a placeholder set that differs from the
English (`{v0}` dropped or renamed), a missing section/status id, or an empty
translation. Also lists strings that are still identical to English and
strings that are much longer than the English, since a 74-column terminal
wraps them badly.

`lang/en.py` is the reference. Every other catalog must define exactly the same
key set.

## selftest.py — exercise the i18n layer

```bash
python3 tools/selftest.py
```

Imports the real `i18n` module, activates every language, and checks running
behaviour: the module loads, catalogs populate, labels resolve, placeholder
substitution leaves nothing behind, unknown keys degrade safely, and the RTL
flag follows the language.

The overlap with `check_translations.py` is deliberate — they fail for different
reasons. `check_translations.py` catches a catalog that is wrong **as data**.
This catches one that looks fine as data but does not survive being loaded and
used: a syntax error, a broken import, a regression in `i18n.py` itself, or a
catalog that silently fails to merge and leaves the program in English.

## i18n_pipeline.py + integrate.py — the migration (run once, already done)

```bash
python3 tools/i18n_pipeline.py     # extract display strings -> lang/en.py
python3 tools/integrate.py         # wire the catalog into hwcheck.py
```

These converted the original plain-English program into the key-based one:

1. `i18n_pipeline.py` walks the AST and replaces every user-visible string with
   `t("key", vN=...)` / `tr("key")`, writing the pristine English source to
   `hwcheck/build/hwcheck_english.py` and the catalog to `hwcheck/lang/en.py`.
   It edits by exact source span rather than `ast.unparse`, which would reflow
   the whole file and destroy comments.
2. `integrate.py` then swaps in the i18n-aware display helpers, the localised
   report renderers and the language selection, and prunes catalog entries the
   final program does not reference.

**You do not need to run these to add a language** — add a catalog. They exist
only if you want to re-extract from a changed English source.

Two pitfalls, both of which cost real debugging time and are guarded by
comments in the source:

* In Python 3.11 an f-string's `format_spec` node **inherits the position of
  the enclosing f-string**, so `ast.get_source_segment` on it returns the whole
  literal instead of `".0f"`. The spec must be rebuilt from the node's own
  constant children.
* The generated call sites must keep the `f` prefix on wrapped format specs
  (`v0=f"{tmax:.0f}"`). Without it the spec becomes a dead string literal and
  the value prints unformatted.

The translator is called `tr()`, not `t()`, because several checker functions
assign to a local `t` (temperatures, thread handles) which would shadow it and
raise `UnboundLocalError` at the call site.