#!/bin/sh
# fx_extract.sh DOCPREFIX OUT.json [CHUNK]: extract the whole timeline in chunks
DOC="$1"; OUT="$2"; CHUNK="${3:-8}"; HERE="$(cd "$(dirname "$0")" && pwd)"
COUNT=$(printf 'doc=[d for d in app.documents if d.name.startswith(%s)][0]\nprint(adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType")).timeline.count)\n' "'$DOC'" > /tmp/f2s_c.py; "$HERE/fx.sh" /tmp/f2s_c.py)
i=0
while [ "$i" -lt "$COUNT" ]; do
  { printf 'DOC=%s\nSTART=%d\nSTOP=%d\nOUT=%s\nADDIN=%s\n' "'$DOC'" "$i" "$((i+CHUNK))" "'$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")'" "'$HERE/../ExportToSolidCraft'"; cat "$HERE/run_extract.py"; } > /tmp/f2s_r.py
  "$HERE/fx.sh" /tmp/f2s_r.py || exit 1
  i=$((i+CHUNK))
done
