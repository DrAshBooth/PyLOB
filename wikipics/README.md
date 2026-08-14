# wikipics

Do not delete these. They look like leftovers from the 2013 tree; they are not.

The GitHub wiki has no file store of its own, so its pages load images from
this repository over `raw.githubusercontent.com`, pinned to `master`. The wiki
rewritten in 2026-08 still uses both:

- `lob_list.jpg` — the `Implementation` page
- `lob_example.jpg` — the `Introduction` page

Verified 2026-08-14 against the live pages. Removing either file, renaming it,
or moving it off `master` breaks an image on a published page, and nothing in
this repository's tests would notice. Check the wiki first.

They are documentation assets, not package data: the wheel already excludes
them (`pyproject.toml` ships `src/PyLOB/**/*.py` and nothing else).
