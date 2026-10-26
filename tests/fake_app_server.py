"""JSONL adapter used only for synthetic end-to-end tests."""
import json
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if mode == "disconnect":
        break
    method = request["method"]
    if method == "initialize":
        result = {"userAgent": "fake-app-server"}
    elif method == "thread/list":
        result = {"data": [], "nextCursor": None}
    else:
        print(json.dumps({"id": request["id"], "error": {"code": -32601, "message": "unsupported"}}), flush=True)
        continue
    print(json.dumps({"method": "test/notification", "params": {}}), flush=True)
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
