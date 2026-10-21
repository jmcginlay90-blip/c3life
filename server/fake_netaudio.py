"""A pretend netaudio for the tests: lists and changes Dante routes and names, kept in the JSON file named by FAKE_DANTE.
    {"subs": [{"rx_device", "rx_channel", "rx_channel_number", "tx_device", "tx_channel"}], "tx": {device: {"1": "01", ...}}, "rx": {...}}
Like Dante, renaming a device or a transmit channel leaves the routes that used the old name broken ("Unresolved")."""
import json
import os
import sys

path = os.environ["FAKE_DANTE"]
with open(path, encoding="utf-8") as f:
    st = json.load(f)
a = [x for x in sys.argv[1:] if x not in ("--json",)]
if "--timeout" in a:
    i = a.index("--timeout")
    del a[i:i + 2]
opt = {a[i]: a[i + 1] for i in range(len(a) - 1) if a[i].startswith("--")}
st.setdefault("rx", {})
st.setdefault("broken", [])
if a[:2] == ["subscription", "list"]:
    print(json.dumps([dict(s, status={"label": "Unresolved" if i in st["broken"] else "Connected"}) for i, s in enumerate(st["subs"])]))
elif a[:2] == ["channel", "list"]:
    print(json.dumps([{"device_name": d, "type": k, "number": int(n), "name": nm} for k in ("tx", "rx") for d, ch in st[k].items() for n, nm in ch.items()]))
elif a[:1] == ["config"]:
    dev = opt["--device-name"]
    if dev not in st["tx"] and dev not in st["rx"]:
        sys.stderr.write("device not found\n")
        sys.exit(2)
    if "--set-device-name" in opt:
        new = opt["--set-device-name"]
        for k in ("tx", "rx"):
            if dev in st[k]:
                st[k][new] = st[k].pop(dev)
        for i, x in enumerate(st["subs"]):
            if x["rx_device"] == dev:
                x["rx_device"] = new
            if x["tx_device"] == dev and i not in st["broken"]:
                st["broken"].append(i)  # still asks for the old name
    else:
        k, num, new = opt["--channel-type"], opt["--channel-number"], opt["--set-channel-name"]
        old = st[k][dev][num]
        st[k][dev][num] = new
        if k == "tx":
            for i, x in enumerate(st["subs"]):
                if x["tx_device"] == dev and x["tx_channel"] == old and i not in st["broken"]:
                    st["broken"].append(i)
    st.setdefault("log", []).append(["config"] + a[1:])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f)
elif a[:2] in (["subscription", "add"], ["subscription", "remove"]):
    rx_dev, rx_ch = opt["--rx-device-name"], opt["--rx-channel-name"]
    keep = [i for i, s in enumerate(st["subs"]) if not (s["rx_device"] == rx_dev and s["rx_channel"] == rx_ch)]
    st["broken"] = [keep.index(i) for i in st["broken"] if i in keep]
    st["subs"] = [st["subs"][i] for i in keep]
    if a[1] == "add":
        if opt["--tx-device-name"] not in st["tx"] or opt["--tx-channel-name"] not in st["tx"][opt["--tx-device-name"]].values() or (rx_dev not in st["rx"] and st["rx"]):
            sys.stderr.write("tx channel not found\n")
            sys.exit(2)
        num = next((int(n) for n, nm in st["rx"].get(rx_dev, {}).items() if nm == rx_ch), None)
        st["subs"].append({"rx_device": rx_dev, "rx_channel": rx_ch, "rx_channel_number": num if num else int(rx_ch), "tx_device": opt["--tx-device-name"], "tx_channel": opt["--tx-channel-name"]})
    st.setdefault("log", []).append(a[:2] + [opt.get("--tx-device-name", ""), opt.get("--tx-channel-name", ""), rx_ch])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f)
else:
    sys.exit(1)
