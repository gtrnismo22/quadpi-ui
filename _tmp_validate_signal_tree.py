"""Temporary validation harness for _build_recursive_signal_tree + chain routing."""
import json
import sys

sys.path.insert(0, r"d:\GitHub\quadpi-ui")

from pipedal_client import PiPedalAsyncClient  # noqa: E402

SPLIT_URI = "uri://two-play/pipedal/pedalboard#Split"


class FakeSignal:
    def __init__(self):
        self.emitted = 0

    def emit(self, *args):
        self.emitted += 1


class FakeBridge:
    def __init__(self):
        self._effect_blocks = []
        self._signal_nodes = []
        self._signal_wires = []
        self._signal_canvas_width = 0
        self._signal_canvas_height = 0
        self.signalTreeChanged = FakeSignal()


def effect(iid, name):
    return {
        "instanceId": iid,
        "name": name,
        "uri": f"uri://example/{name}",
        "category": "Utility",
        "color": "#10B981",
        "enabled": True,
        "bypassed": False,
        "controls": [],
        "fileProperties": [],
        "pathProperties": {},
    }


def split(iid, top, bottom):
    return {
        "instanceId": iid,
        "uri": SPLIT_URI,
        "isEnabled": True,
        "controlValues": [{"key": "splitType", "value": 0.0}, {"key": "select", "value": 0.0}],
        "topChain": top,
        "bottomChain": bottom,
    }


def node(iid):
    return {"instanceId": iid, "uri": f"uri://example/{iid}", "isEnabled": True, "controlValues": []}


def make_client(pedalboard):
    client = object.__new__(PiPedalAsyncClient)
    client.bridge = FakeBridge()
    client.raw_pedalboard = pedalboard
    client.client_id = 1
    client.ui_plugins_by_uri = {}
    client.sent = []
    client._schedule_coro = lambda coro: client.sent.append(coro)
    return client


# main: E1 -> S1( top: [T1, S2(top:[T2a, T2b], bottom:[B2a])], bottom: [B1] ) -> E2
items = [
    node(1),
    split(100, [node(11), split(101, [node(21), node(22)], [node(23)])], [node(12)]),
    node(2),
]
raw = {"items": items, "nextInstanceId": 500}

by_id = {1: effect(1, "E1"), 2: effect(2, "E2")}
for iid, name in ((11, "T1"), (21, "T2a"), (22, "T2b"), (23, "B2a"), (12, "B1")):
    by_id[iid] = effect(iid, name)

client = make_client(raw)
client._build_recursive_signal_tree(json.loads(json.dumps(items)), by_id)

nodes = client.bridge._signal_nodes
wires = client.bridge._signal_wires
print(f"nodes={len(nodes)} wires={len(wires)} canvas={client.bridge._signal_canvas_width}x{client.bridge._signal_canvas_height}")

expected_chain = {
    1: "main", 2: "main",
    100: "main",
    11: "split:100:top", 12: "split:100:bottom",
    101: "split:100:top",
    21: "split:101:top", 22: "split:101:top", 23: "split:101:bottom",
}
chain_ok = True
for n in nodes:
    iid = n.get("instanceId")
    if iid in expected_chain:
        if n.get("chainId") != expected_chain[iid]:
            chain_ok = False
            print(f"  BAD chainId node {iid}: {n.get('chainId')!r} != {expected_chain[iid]!r}")
    elif iid in (-10, -11) and n.get("chainId") != "main":
        chain_ok = False
        print(f"  BAD capsule chainId {iid}: {n.get('chainId')!r}")
print("chainId strings:", "OK" if chain_ok else "FAIL")

slot_chains = sorted({n["chainId"] for n in nodes if n.get("kind") == "addSlot"})
print("addSlot chainIds:", slot_chains)
print("slot chainIds valid:", set(slot_chains) <= set(expected_chain.values()))

blocks = [n for n in nodes if n.get("kind") in ("effect", "split")]
overlaps = []
for i, a in enumerate(blocks):
    for b in blocks[i + 1:]:
        if (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
                and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]):
            overlaps.append((a["instanceId"], b["instanceId"]))
print("block overlaps:", overlaps if overlaps else "none")

ys = {n["instanceId"]: n["y"] + n["height"] / 2.0 for n in blocks}
print("lane centers:", {k: round(v, 1) for k, v in sorted(ys.items())})
assert ys[21] < ys[100] < ys[12], "nested top branch must stay above parent bottom branch"
assert ys[23] < ys[12], "nested bottom branch of S2 must stay above S1 bottom branch"
print("nested ordering: OK")
