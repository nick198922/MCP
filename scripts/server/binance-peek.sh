#!/bin/bash
# From the Mac: header, row count and three rows of each downloaded Binance history file (read-only).
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cd /var/lib/tradedesk-mcp/journal/binance && for z in *.zip; do echo "== $z";
python3 -I -c "
import sys, zipfile, io
z = zipfile.ZipFile(sys.argv[1])
for n in z.namelist():
    t = z.read(n).decode(\"utf-8\", \"replace\").splitlines()
    print(n, len(t), \"lines\"); print(\"\\n\".join(t[:4]))
" "$z"; done'
