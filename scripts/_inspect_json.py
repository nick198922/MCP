"""Temporary helper: print the shape of a JSON file, or a selected element as JSON. Not part of the project.

python scripts/_inspect_json.py FILE [DEPTH]          shape
python scripts/_inspect_json.py FILE --get a.b.0 [N]  JSON of an element (lists: first N items)
"""

import json
import sys


def sh(x, dep=0, maxdep=5):
    if dep > maxdep:
        return
    if isinstance(x, dict):
        for k, v in list(x.items())[:40]:
            if isinstance(v, (str, int, float, bool)) or v is None:
                print("  " * dep + f"{k}: {repr(v)[:160]}")
            else:
                print("  " * dep + f"{k}: {type(v).__name__}[{len(v)}]")
                sh(v, dep + 1, maxdep)
    elif isinstance(x, list) and x:
        if isinstance(x[0], (dict, list)):
            sh(x[0], dep + 1, maxdep)
        else:
            print("  " * dep + repr(x[:5])[:200])


d = json.load(open(sys.argv[1]))
if len(sys.argv) > 2 and sys.argv[2] == "--get":
    for part in sys.argv[3].split(".") if sys.argv[3] else []:
        d = d[int(part)] if isinstance(d, list) else d[part]
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    print(json.dumps(d[:n] if isinstance(d, list) else d, indent=1)[:6000])
else:
    sh(d, 0, int(sys.argv[2]) if len(sys.argv) > 2 else 5)
