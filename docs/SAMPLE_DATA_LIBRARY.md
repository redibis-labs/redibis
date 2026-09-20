# Sample data library — scan a file already on the server

Lets an operator start a scan from a file that already sits on the machine
running redibis, instead of uploading one through the browser. Useful for
demo corpora, shared test fixtures, and files too large to push through an
upload form.

**Default roots are `redibis/webapp/samples` and, in a source checkout,
`tests/data`.** Override with `REDIBIS_SAMPLE_DATA_DIR` or `sample_data.roots`.
Every configured root becomes readable to every signed-in user of the web app,
so treat adding one as granting read access to that directory.

---

## Override the default

Quickest, for a single directory:

```bash
REDIBIS_SAMPLE_DATA_DIR=/srv/redibis/samples redibis serve
```

On Windows, and for several directories, separate with `;`:

```
REDIBIS_SAMPLE_DATA_DIR=D:\data\samples;D:\data\demo
```

For anything beyond one directory, put it in `REDIBIS_CONFIG`:

```yaml
sample_data:
  roots:
    - /srv/redibis/samples                 # name derived from the directory
    - name: demo                           # or name it yourself
      label: Demo corpora
      path: /srv/redibis/demo
  extensions: [".csv", ".tsv", ".parquet", ".xlsx", ".xls"]
  max_file_mb: 512      # files above this are listed but not loadable
  max_depth: 4          # how deep the picker walks
  max_entries: 2000     # listing cap
  allow_scan: true      # false = browse and preview only, no scanning
```

Roots from the environment variable come first and are named `env`, `env2`, …
Config roots keep the name you give them, or one derived from the directory.

## Use it

On the scan console the path box defaults to
`tests/data/realistic_eshop_customer_account.csv` (shipped in OSS and
enterprise). **upload** picks a CSV from this computer. Type a CSV **name**
(`customers.csv` → first matching sample root), a checkout-relative path, or a
**full path** that still sits inside a configured root. The start page prints
those root paths. **browse** lists CSV files on the server; **use →** (or Enter)
loads columns from a server-side preview. A local upload and a server path both
create the same kind of scan session. Non-CSV names are refused in the UI.

## What is readable, and what is not

A path is only readable when **all** of these hold:

1. It resolves inside a configured root (a filename is joined to the root; a
   checkout-relative path such as ``tests/data/file.csv`` is resolved from the
   process working directory or the source checkout, then accepted only when
   it still sits inside a root; an absolute path is accepted only when it
   still resolves inside a root).
   Resolution happens **before** the containment check, so a symlink pointing
   outside the root resolves outside it and is refused. Directory symlinks are
   skipped during listing for the same reason.
2. No path component starts with `.` — no dotfiles, and `..` cannot survive.
   A lone `.` is a harmless no-op and is normalized away.
3. The suffix is in `extensions`.
4. It is a regular file within `max_file_mb`.

`redibis/services/sample_data.py::resolve` is the only place that decides
this. The HTTP layer never builds a path itself, so there is one gate to
audit rather than three. `tests/test_sample_data.py` covers each escape
technique separately.

Starting a scan is a mutating request: it needs a session, passes CSRF, and
is refused for the read-only `explorer` role, exactly like an upload.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/sample-data[?root=NAME]` | List loadable files. With no env/config, lists `redibis/webapp/samples`. `{"enabled": false}` only if that directory cannot be used. |
| `GET /api/sample-data/preview?path=REL[&root=NAME][&rows=20]` | Column names, dtypes, first rows, row count. `path` may be a name under the root or an absolute path inside it. |
| `POST /api/sessions/from-sample` | Create a scan session from a server file. Body mirrors `POST /api/sessions` with `root`/`path` in place of the upload. |

`row_count` is exact for CSV/TSV under 64 MB and for Parquet; above that it
is `null` and `row_count_exact` is `false`. Excel previews do not count rows.

## Notes

- A preview returns real values from the file. That is the point of the
  feature, but it means configuring a root over a directory of production
  extracts exposes those values to every signed-in user. Point roots at
  sample and test data.
- `allow_scan: false` leaves listing and preview working while blocking
  session creation — useful when you want the picker visible but read-only.
